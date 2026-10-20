"""Find Bambu Lab printers on the LAN: they announce themselves over SSDP on UDP 2021."""

from __future__ import annotations

import asyncio
import socket

SSDP_PORT = 2021
MODELS = {
    "C11": "P1P", "C12": "P1S", "C13": "X1E", "BL-P001": "X1 Carbon", "BL-P002": "X1",
    "N1": "A1 mini", "N2S": "A1", "O1D": "H2D", "N7": "P2S",
}


def parse_notify(payload: bytes, sender: str) -> dict | None:
    text = payload.decode(errors="replace")
    if "bambu" not in text.lower():
        return None
    headers = {}
    for line in text.splitlines()[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    serial = headers.get("usn", "")
    if not serial:
        return None
    code = headers.get("devmodel.bambu.com", "")
    return {"host": headers.get("location") or sender, "serial": serial, "name": headers.get("devname.bambu.com", ""),
            "model": MODELS.get(code, code), "connect": headers.get("devconnect.bambu.com", "")}


class _Listener(asyncio.DatagramProtocol):
    def __init__(self, found: dict):
        self.found = found

    def datagram_received(self, data: bytes, addr) -> None:
        printer = parse_notify(data, addr[0])
        if printer:
            self.found[printer["serial"]] = printer


async def discover(timeout: float = 6.0) -> list[dict]:
    """Printers that announced themselves within `timeout` seconds (they do every few seconds)."""
    found: dict[str, dict] = {}
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        sock.bind(("", SSDP_PORT))
    except OSError as exc:
        sock.close()
        raise RuntimeError(f"cannot listen on UDP {SSDP_PORT} for printer announcements: {exc}") from exc
    transport, _ = await loop.create_datagram_endpoint(lambda: _Listener(found), sock=sock)
    try:
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return sorted(found.values(), key=lambda p: p["name"] or p["serial"])
