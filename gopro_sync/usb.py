"""Detect GoPro cameras attached over USB (they show up as a USB network interface)."""

from __future__ import annotations

import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

import psutil

from . import netutil

GOPRO_USB_VENDOR = "2672"
# The camera serves 172.2X.1YZ.51 and leases the host an address in the same /24.
USB_ADDRESS = re.compile(r"^172\.2\d\.1\d\d\.\d{1,3}$")


@dataclass(frozen=True)
class UsbCamera:
    interface: str
    host_ip: str

    @property
    def camera_ip(self) -> str:
        return self.host_ip.rsplit(".", 1)[0] + ".51"


def _gopro_interfaces_linux() -> list[str]:
    found = []
    for net in Path("/sys/class/net").iterdir():
        device = net / "device"
        if not device.exists():
            continue
        node = device.resolve()
        for _ in range(6):
            vendor = node / "idVendor"
            if vendor.exists():
                if vendor.read_text().strip() == GOPRO_USB_VENDOR:
                    found.append(net.name)
                break
            node = node.parent
    return found


async def discover(run_dir: Path) -> list[UsbCamera]:
    if sys.platform == "linux":
        # Linux (netplan/networkd) won't configure the camera's interface on its own.
        for iface in _gopro_interfaces_linux():
            if not await netutil.ipv4_addresses(iface):
                try:
                    await netutil.dhcp_lease(iface, run_dir)
                except netutil.CommandError:
                    pass
    cameras = []
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET and USB_ADDRESS.match(addr.address) and not addr.address.endswith(".51"):
                cameras.append(UsbCamera(iface, addr.address))
    return cameras
