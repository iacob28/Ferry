"""Find network storage on the LAN (mDNS + port sweep) and list its shares/exports."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
import tempfile
from dataclasses import dataclass, field

import psutil

MDNS_SERVICES = {
    "_smb._tcp.local.": "smb",
    "_nfs._tcp.local.": "nfs",
    "_afpovertcp._tcp.local.": "afp",
    "_webdav._tcp.local.": "webdav",
    "_sftp-ssh._tcp.local.": "sftp",
}
SWEEP_PORTS = {445: "smb", 2049: "nfs", 548: "afp"}
CONNECT_TIMEOUT = 0.6
SWEEP_CONCURRENCY = 256
MAX_SWEEP_HOSTS = 1024
MIN_PREFIX = 22
SKIP_IFACE_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "tun", "utun", "tailscale", "wg", "zt")
GOPRO_USB_RE = re.compile(r"^172\.2\d\.1\d\d\.")
RDNS_TIMEOUT = 1.5

SMB_ERRORS = {
    "NT_STATUS_LOGON_FAILURE": "authentication failed",
    "NT_STATUS_ACCESS_DENIED": "access denied",
    "NT_STATUS_HOST_UNREACHABLE": "host unreachable",
    "NT_STATUS_IO_TIMEOUT": "host unreachable",
    "NT_STATUS_CONNECTION_REFUSED": "host unreachable",
}


class DiscoveryError(Exception):
    pass


@dataclass
class NetworkHost:
    address: str
    name: str | None = None
    services: list[str] = field(default_factory=list)


# --- interface / subnet selection -------------------------------------------------


def _sweep_networks(addrs: dict, stats: dict) -> list[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address]]:
    """Pick (subnet, own address) pairs worth sweeping from psutil-style interface data."""
    nets: list[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address]] = []
    seen: set[ipaddress.IPv4Network] = set()
    for iface, entries in addrs.items():
        if iface.lower().startswith(SKIP_IFACE_PREFIXES):
            continue
        st = stats.get(iface)
        if st is None or not st.isup:
            continue
        for a in entries:
            if a.family != socket.AF_INET or not a.address or not a.netmask:
                continue
            if GOPRO_USB_RE.match(a.address):
                continue
            try:
                iface_net = ipaddress.IPv4Interface(f"{a.address}/{a.netmask}")
            except ValueError:
                continue
            ip, net = iface_net.ip, iface_net.network
            if not ip.is_private or ip.is_loopback or ip.is_link_local:
                continue
            if net.prefixlen < MIN_PREFIX or net.prefixlen > 30 or net in seen:
                continue
            seen.add(net)
            nets.append((net, ip))
    return nets


def _sweep_targets(nets) -> list[str]:
    targets: list[str] = []
    for net, own in nets:
        hosts = [h for h in net.hosts() if h != own][:MAX_SWEEP_HOSTS]
        targets.extend(str(h) for h in hosts)
    return list(dict.fromkeys(targets))


# --- sweep ------------------------------------------------------------------------


async def _probe(address: str, port: int, sem: asyncio.Semaphore) -> bool:
    async with sem:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(address, port), CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 0.2)
        except (OSError, asyncio.TimeoutError):
            pass
        return True


async def _sweep(found: dict[str, set[str]]) -> None:
    try:
        nets = _sweep_networks(psutil.net_if_addrs(), psutil.net_if_stats())
    except Exception:
        return
    sem = asyncio.Semaphore(SWEEP_CONCURRENCY)

    async def one(address: str, port: int, service: str) -> None:
        if await _probe(address, port, sem):
            found.setdefault(address, set()).add(service)

    targets = _sweep_targets(nets)
    await asyncio.gather(*(one(a, p, s) for p, s in SWEEP_PORTS.items() for a in targets))


# --- mDNS -------------------------------------------------------------------------


def _instance_name(full: str, service_type: str) -> str:
    name = full[: -len(service_type)] if full.endswith(service_type) else full
    return name.rstrip(".")


async def _mdns(found: dict[str, tuple[str | None, set[str]]], timeout: float) -> None:
    try:
        from zeroconf import IPVersion, ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError:
        return

    pending: set[asyncio.Task] = set()

    async def resolve(zc, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zc, 1500):
            return
        friendly = _instance_name(name, service_type) or (info.server or "").removesuffix(".").removesuffix(".local") or None
        for addr in info.parsed_addresses(IPVersion.V4Only):
            ip = ipaddress.IPv4Address(addr)
            if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                continue
            prev_name, services = found.setdefault(addr, (None, set()))
            services.add(MDNS_SERVICES[service_type])
            if prev_name is None and friendly:
                found[addr] = (friendly, services)

    def on_change(zeroconf, service_type, name, state_change) -> None:
        if state_change is ServiceStateChange.Added:
            task = asyncio.ensure_future(resolve(azc.zeroconf, service_type, name))
            pending.add(task)
            task.add_done_callback(pending.discard)

    try:
        azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    except Exception:
        return
    browser = None
    try:
        browser = AsyncServiceBrowser(azc.zeroconf, list(MDNS_SERVICES), handlers=[on_change])
        await asyncio.sleep(timeout)
        if pending:
            await asyncio.wait(list(pending), timeout=1.0)
    except Exception:
        pass
    finally:
        for task in list(pending):
            task.cancel()
        try:
            if browser is not None:
                await browser.async_cancel()
            await asyncio.wait_for(azc.async_close(), 1.0)
        except Exception:
            pass


# --- merge ------------------------------------------------------------------------


def _merge(mdns: dict[str, tuple[str | None, set[str]]], sweep: dict[str, set[str]]) -> list[NetworkHost]:
    hosts: dict[str, NetworkHost] = {}
    for addr, (name, services) in mdns.items():
        hosts[addr] = NetworkHost(addr, name, sorted(services))
    for addr, services in sweep.items():
        host = hosts.setdefault(addr, NetworkHost(addr))
        host.services = sorted(set(host.services) | services)
    return _sort_hosts(list(hosts.values()))


def _sort_hosts(hosts: list[NetworkHost]) -> list[NetworkHost]:
    return sorted(
        hosts,
        key=lambda h: (h.name is None, (h.name or "").lower(), int(ipaddress.IPv4Address(h.address))),
    )


async def _reverse_dns(address: str) -> str | None:
    try:
        name, _, _ = await asyncio.wait_for(asyncio.to_thread(socket.gethostbyaddr, address), RDNS_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return None
    return name or None


async def discover_hosts(timeout: float = 4.0) -> list[NetworkHost]:
    """Find hosts offering file services: mDNS browse plus a TCP sweep of local subnets."""
    mdns_found: dict[str, tuple[str | None, set[str]]] = {}
    sweep_found: dict[str, set[str]] = {}
    tasks = [
        asyncio.ensure_future(_mdns(mdns_found, timeout)),
        asyncio.ensure_future(_sweep(sweep_found)),
    ]
    _, still = await asyncio.wait(tasks, timeout=timeout + 1.0)
    for task in still:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    hosts = _merge(mdns_found, sweep_found)
    unnamed = [h for h in hosts if h.name is None]
    if unnamed:
        names = await asyncio.gather(*(_reverse_dns(h.address) for h in unnamed))
        for host, name in zip(unnamed, names):
            host.name = name
        hosts = _sort_hosts(hosts)
    return hosts


# --- share / export listing -------------------------------------------------------


def _check_host(host: str) -> str:
    host = host.strip()
    if not host or host.startswith("-") or any(c.isspace() or c in "/\\" for c in host):
        raise DiscoveryError(f"invalid host: {host!r}")
    return host


def _parse_smbclient_output(text: str) -> list[dict]:
    """Parse `smbclient -L -g` output into Disk shares; raise on a reported failure."""
    shares: list[dict] = []
    any_share = False
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 2 and parts[0] in ("Disk", "IPC", "Printer"):
            any_share = True
            name = parts[1]
            if parts[0] == "Disk" and name and not name.endswith("$"):
                shares.append({"name": name, "comment": "|".join(parts[2:]).strip()})
    if any_share:
        return sorted(shares, key=lambda s: s["name"].lower())
    for status, message in SMB_ERRORS.items():
        if status in text:
            raise DiscoveryError(message)
    m = re.search(r"NT_STATUS_[A-Z_]+", text)
    if m:
        raise DiscoveryError(f"share listing failed ({m.group(0)})")
    return []


def _smb_auth_file(username: str, password: str) -> str:
    domain = None
    if "\\" in username:
        domain, username = username.split("\\", 1)
    lines = [f"username={username}", f"password={password}"]
    if domain:
        lines.append(f"domain={domain}")
    fd, path = tempfile.mkstemp(prefix="gopro-sync-smb-")
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, ("\n".join(lines) + "\n").encode())
    finally:
        os.close(fd)
    return path


async def _exec(args: list[str], timeout: float, missing: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        raise DiscoveryError(missing) from None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DiscoveryError("host unreachable (timed out)") from None
    return proc.returncode or 0, out.decode(errors="replace")


async def list_smb_shares(host: str, username: str = "", password: str = "", timeout: float = 15.0) -> list[dict]:
    """List the visible Disk shares on an SMB server via the smbclient CLI."""
    host = _check_host(host)
    args = ["smbclient", "-L", f"//{host}", "-g", "-m", "SMB3"]
    auth = None
    if username:
        auth = _smb_auth_file(username, password)
        args += ["-A", auth]
    else:
        args.append("-N")
    try:
        code, text = await _exec(args, timeout, "share listing needs the smbclient tool (included in the Docker image)")
    finally:
        if auth:
            try:
                os.unlink(auth)
            except OSError:
                pass
    shares = _parse_smbclient_output(text)
    if not shares and code != 0 and "|" not in text:
        raise DiscoveryError(f"share listing failed: {text.strip().splitlines()[-1] if text.strip() else code}")
    return shares


def _parse_showmount(text: str) -> list[str]:
    """Parse `showmount -e` output into export paths; raise on an RPC failure."""
    paths: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("/"):
            continue
        parts = line.rsplit(None, 1)
        paths.append(parts[0] if len(parts) == 2 else line)
    if paths:
        return list(dict.fromkeys(paths))
    low = text.lower()
    if "program not registered" in low:
        raise DiscoveryError("NFS is not enabled on this host")
    if "access denied" in low or "permission denied" in low:
        raise DiscoveryError("access denied")
    if any(s in low for s in ("unable to receive", "unable to send", "timed out", "port mapper", "no route", "unreachable", "refused", "unknown host")):
        raise DiscoveryError("host unreachable")
    return []


async def list_nfs_exports(host: str, timeout: float = 10.0) -> list[str]:
    """List NFS export paths via showmount."""
    host = _check_host(host)
    code, text = await _exec(
        ["showmount", "-e", host],
        timeout,
        "export listing needs the showmount tool from nfs-common (included in the Docker image)",
    )
    exports = _parse_showmount(text)
    if not exports and code != 0:
        raise DiscoveryError(f"export listing failed: {text.strip().splitlines()[-1] if text.strip() else code}")
    return exports
