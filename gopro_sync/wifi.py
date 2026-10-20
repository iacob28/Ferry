"""Join the camera's Wi-Fi access point on a dedicated radio (Linux only)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from . import netutil


class WifiError(Exception):
    pass


class WifiLink:
    def __init__(self, iface: str, run_dir: Path):
        self.iface = iface
        self.run_dir = run_dir / "wifi" / iface
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._pidfile = self.run_dir / "wpa_supplicant.pid"
        self._ctrl = self.run_dir / "ctrl"

    async def ensure_safe(self) -> None:
        """Refuse to touch a radio that carries the LAN (the Pi serves DNS for the house)."""
        if sys.platform != "linux":
            raise WifiError("wireless offload is only supported on Linux")
        if not Path(f"/sys/class/net/{self.iface}").exists():
            raise WifiError(f"interface {self.iface} does not exist")
        if self.iface in await netutil.default_route_interfaces():
            raise WifiError(
                f"{self.iface} carries the default route; connect the host over Ethernet "
                "or choose a second Wi-Fi adapter"
            )
        foreign = [a for a in await netutil.ipv4_addresses(self.iface) if not a.startswith("10.5.5.")]
        if foreign:
            raise WifiError(f"{self.iface} is in use by another network ({', '.join(foreign)})")

    async def connect(self, ssid: str, password: str, timeout: float = 45.0) -> None:
        await self.ensure_safe()
        await self.disconnect()
        conf = self.run_dir / "wpa_supplicant.conf"
        conf.write_text(
            f"ctrl_interface=DIR={self._ctrl}\n"
            "network={\n"
            f"    ssid={ssid.encode().hex()}\n"
            f"    psk=\"{password}\"\n"
            "    scan_ssid=1\n"
            "}\n"
        )
        os.chmod(conf, 0o600)
        await netutil.run("ip", "link", "set", self.iface, "up")
        await netutil.run("wpa_supplicant", "-B", "-i", self.iface, "-c", str(conf), "-P", str(self._pidfile))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            status = await netutil.run("wpa_cli", "-p", str(self._ctrl), "-i", self.iface, "status", check=False)
            if "wpa_state=COMPLETED" in status:
                break
            if loop.time() > deadline:
                await self.disconnect()
                raise WifiError(f"could not join '{ssid}' within {timeout:.0f}s")
            await asyncio.sleep(1)
        await netutil.dhcp_lease(self.iface, self.run_dir)

    async def disconnect(self) -> None:
        if self._pidfile.exists():
            try:
                os.kill(int(self._pidfile.read_text().strip()), signal.SIGTERM)
            except (ValueError, ProcessLookupError):
                pass
            self._pidfile.unlink(missing_ok=True)
            await asyncio.sleep(1)
        if sys.platform == "linux":
            # Only drop addresses from the camera's network, never anything else on the radio.
            for addr in await netutil.ipv4_addresses(self.iface):
                if addr.startswith("10.5.5."):
                    await netutil.run("ip", "addr", "del", f"{addr}/24", "dev", self.iface, check=False)
