from __future__ import annotations

import ipaddress
import os
import socket
from types import SimpleNamespace

import pytest

from ferry import discovery
from ferry.discovery import (
    DiscoveryError,
    NetworkHost,
    _merge,
    _parse_showmount,
    _parse_smbclient_output,
    _smb_auth_file,
    _sweep_networks,
    _sweep_targets,
)


def addr(address, netmask, family=socket.AF_INET):
    return SimpleNamespace(family=family, address=address, netmask=netmask)


def up(isup=True):
    return SimpleNamespace(isup=isup)


def test_sweep_networks_selects_private_lan_only():
    addrs = {
        "lo0": [addr("127.0.0.1", "255.0.0.0")],
        "en0": [addr("192.168.0.10", "255.255.255.0"), addr("fe80::1", None, socket.AF_INET6)],
        "docker0": [addr("172.17.0.1", "255.255.0.0")],
        "br-abc": [addr("172.18.0.1", "255.255.255.0")],
        "veth1": [addr("10.1.0.1", "255.255.255.0")],
        "tailscale0": [addr("100.64.0.1", "255.255.255.255")],
        "usb0": [addr("172.25.190.51", "255.255.255.0")],
        "eth1": [addr("10.0.0.5", "255.255.0.0")],
        "eth2": [addr("10.9.0.5", "255.255.252.0")],
        "wlan0": [addr("192.168.5.2", "255.255.255.0")],
        "eth3": [addr("8.8.8.8", "255.255.255.0")],
        "eth4": [addr("169.254.3.3", "255.255.255.0")],
    }
    stats = {k: up() for k in addrs}
    stats["wlan0"] = up(False)
    nets = _sweep_networks(addrs, stats)
    assert nets == [
        (ipaddress.IPv4Network("192.168.0.0/24"), ipaddress.IPv4Address("192.168.0.10")),
        (ipaddress.IPv4Network("10.9.0.0/22"), ipaddress.IPv4Address("10.9.0.5")),
    ]


def test_sweep_networks_missing_stats_and_dedup():
    addrs = {"en0": [addr("192.168.0.10", "255.255.255.0")], "en1": [addr("192.168.0.11", "255.255.255.0")], "en2": [addr("192.168.1.1", "255.255.255.0")]}
    nets = _sweep_networks(addrs, {"en0": up(), "en1": up()})
    assert [str(n) for n, _ in nets] == ["192.168.0.0/24"]


def test_sweep_targets_skips_own_and_caps():
    nets = [(ipaddress.IPv4Network("192.168.0.0/24"), ipaddress.IPv4Address("192.168.0.10"))]
    targets = _sweep_targets(nets)
    assert len(targets) == 253 and "192.168.0.10" not in targets and targets[0] == "192.168.0.1"
    big = [(ipaddress.IPv4Network("10.0.0.0/22"), ipaddress.IPv4Address("10.0.0.1"))]
    assert len(_sweep_targets(big)) == 1021


def test_merge_and_sort():
    mdns = {"192.168.0.2": ("SynologyStorage", {"smb", "afp"}), "192.168.0.40": ("alpha", {"webdav"})}
    sweep = {"192.168.0.2": {"smb", "nfs"}, "192.168.0.100": {"smb"}, "192.168.0.9": {"nfs"}}
    hosts = _merge(mdns, sweep)
    assert hosts == [
        NetworkHost("192.168.0.40", "alpha", ["webdav"]),
        NetworkHost("192.168.0.2", "SynologyStorage", ["afp", "nfs", "smb"]),
        NetworkHost("192.168.0.9", None, ["nfs"]),
        NetworkHost("192.168.0.100", None, ["smb"]),
    ]


def test_instance_name():
    assert discovery._instance_name("SynologyStorage._smb._tcp.local.", "_smb._tcp.local.") == "SynologyStorage"


async def test_discover_hosts_merges_and_names(monkeypatch):
    async def fake_mdns(found, timeout, ports):
        found["192.168.0.2"] = ("SynologyStorage", {"smb", "sftp"})
        ports["192.168.0.2"] = {"sftp": 2123}

    async def fake_sweep(found):
        found["192.168.0.2"] = {"smb", "afp"}
        found["192.168.0.3"] = {"nfs"}
        found["192.168.0.4"] = {"smb"}

    async def fake_rdns(address):
        return "pi.lan" if address == "192.168.0.3" else None

    monkeypatch.setattr(discovery, "_mdns", fake_mdns)
    monkeypatch.setattr(discovery, "_sweep", fake_sweep)
    monkeypatch.setattr(discovery, "_reverse_dns", fake_rdns)
    hosts = await discovery.discover_hosts(timeout=0.1)
    assert [(h.address, h.name, h.services) for h in hosts] == [
        ("192.168.0.3", "pi.lan", ["nfs"]),
        ("192.168.0.2", "SynologyStorage", ["afp", "sftp", "smb"]),
        ("192.168.0.4", None, ["smb"]),
    ]
    assert hosts[1].ports == {"sftp": 2123} and hosts[0].ports == {}


SMB_OK = """Reconnecting with SMB1 for workgroup listing.
Disk|photo|
Disk|video|Video library
IPC|IPC$|IPC Service (Synology)
Disk|home|home|with pipe
Disk|admin$|hidden
Printer|laser|
do_connect: Connection to 192.168.0.2 failed (Error NT_STATUS_CONNECTION_REFUSED)
Unable to connect with SMB1 -- no workgroup available
"""


def test_parse_smbclient_shares():
    assert _parse_smbclient_output(SMB_OK) == [
        {"name": "home", "comment": "home|with pipe"},
        {"name": "photo", "comment": ""},
        {"name": "video", "comment": "Video library"},
    ]


@pytest.mark.parametrize(
    "text,msg",
    [
        ("session setup failed: NT_STATUS_LOGON_FAILURE", "authentication failed"),
        ("tree connect failed: NT_STATUS_ACCESS_DENIED", "access denied"),
        ("do_connect: Connection to x failed (Error NT_STATUS_HOST_UNREACHABLE)", "host unreachable"),
        ("do_connect: Connection to x failed (Error NT_STATUS_IO_TIMEOUT)", "host unreachable"),
        ("do_connect: Connection to x failed (Error NT_STATUS_CONNECTION_REFUSED)", "host unreachable"),
        ("something NT_STATUS_BAD_NETWORK_NAME", "NT_STATUS_BAD_NETWORK_NAME"),
    ],
)
def test_parse_smbclient_errors(text, msg):
    with pytest.raises(DiscoveryError, match=msg):
        _parse_smbclient_output(text)


def test_parse_smbclient_empty():
    assert _parse_smbclient_output("IPC|IPC$|IPC Service\n") == []


def test_smb_auth_file():
    path = _smb_auth_file("WORKGROUP\\bob", "s3cret")
    try:
        assert os.stat(path).st_mode & 0o777 == 0o600
        with open(path) as f:
            assert f.read() == "username=bob\npassword=s3cret\ndomain=WORKGROUP\n"
    finally:
        os.unlink(path)


async def test_list_smb_shares_missing_tool(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(DiscoveryError, match="smbclient"):
        await discovery.list_smb_shares("192.168.0.2", "bob", "pw")


async def test_list_smb_shares_never_puts_password_on_cmdline(monkeypatch):
    seen = {}

    async def fake_exec(args, timeout, missing):
        seen["args"] = args
        auth = args[args.index("-A") + 1]
        with open(auth) as f:
            seen["auth"] = f.read()
        return 0, "Disk|photo|\n"

    monkeypatch.setattr(discovery, "_exec", fake_exec)
    shares = await discovery.list_smb_shares("nas.local", "bob", "s3cret")
    assert shares == [{"name": "photo", "comment": ""}]
    assert "s3cret" not in " ".join(seen["args"]) and "password=s3cret" in seen["auth"]
    assert not os.path.exists(seen["args"][seen["args"].index("-A") + 1])


async def test_invalid_host_rejected():
    with pytest.raises(DiscoveryError, match="invalid host"):
        await discovery.list_nfs_exports("-o foo")


def test_parse_showmount():
    text = "/volume1/gopro   192.168.0.0/24\n/volume1/My Videos *\n/export\n"
    assert _parse_showmount(text) == ["/volume1/gopro", "/volume1/My Videos", "/export"]
    assert _parse_showmount("Export list for nas:\n/srv  *\n") == ["/srv"]
    assert _parse_showmount("") == []


@pytest.mark.parametrize(
    "text,msg",
    [
        ("clnt_create: RPC: Program not registered", "not enabled"),
        ("clnt_create: RPC: Unable to receive", "unreachable"),
        ("rpc mount export: RPC: Authentication error; why = Access denied", "access denied"),
    ],
)
def test_parse_showmount_errors(text, msg):
    with pytest.raises(DiscoveryError, match=msg):
        _parse_showmount(text)


async def test_list_nfs_exports_missing_tool(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(DiscoveryError, match="nfs-common"):
        await discovery.list_nfs_exports("192.168.0.2")
