"""Small helpers for driving Linux networking tools."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

# udhcpc hook: assign the leased address only. GoPro leases advertise the camera as
# router and DNS server; installing those would hijack the host's default route.
UDHCPC_SCRIPT = """#!/bin/sh
case "$1" in
  deconfig) ip -4 addr flush dev "$interface" ;;
  bound|renew)
    ip -4 addr flush dev "$interface"
    ip addr add "$ip/${mask:-24}" dev "$interface"
    ;;
esac
exit 0
"""


class CommandError(Exception):
    pass


async def run(*args: str, check: bool = True, timeout: float = 60.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise CommandError(f"{args[0]} timed out") from None
    text = out.decode(errors="replace")
    if check and proc.returncode != 0:
        raise CommandError(f"{' '.join(args)} exited {proc.returncode}: {text.strip()}")
    return text


async def default_route_interfaces() -> set[str]:
    out = await run("ip", "-j", "route", "show", "default")
    return {r["dev"] for r in json.loads(out or "[]") if "dev" in r}


async def ipv4_addresses(iface: str) -> list[str]:
    out = await run("ip", "-j", "-4", "addr", "show", "dev", iface, check=False)
    try:
        entries = json.loads(out or "[]")
    except json.JSONDecodeError:
        return []
    return [a["local"] for e in entries for a in e.get("addr_info", [])]


def udhcpc_script(run_dir: Path) -> Path:
    path = run_dir / "udhcpc.sh"
    if not path.exists() or path.read_text() != UDHCPC_SCRIPT:
        path.write_text(UDHCPC_SCRIPT)
        os.chmod(path, 0o755)
    return path


async def dhcp_lease(iface: str, run_dir: Path) -> None:
    """Obtain an address on iface without touching routes or DNS."""
    await run("ip", "link", "set", iface, "up")
    await run("udhcpc", "-i", iface, "-n", "-q", "-t", "10", "-T", "2", "-s", str(udhcpc_script(run_dir)))
