"""Watch for GoPro cameras over USB and BLE and offload them."""

from __future__ import annotations

import asyncio
import sys
import time

from ...core.session import Flow, Session
from ...core.settings import PairedCamera, Settings
from ..base import Provider
from . import ble, usb
from .camera import WIFI_AP_HOST, GoProHttp
from .wifi import WifiLink

USB_POLL_SECONDS = 3
BLE_SCAN_SECONDS = 6
# Back-off after a failed wireless attempt, so a flaky camera isn't hammered.
BLE_FAILURE_COOLDOWN = 5 * 60
LOW_BATTERY = 15


class GoProProvider(Provider):
    kind = "gopro"
    label = "GoPro"

    def __init__(self, service):
        super().__init__(service)
        self.usb_cameras: list[usb.UsbCamera] = []
        self.ble_seen: dict[str, dict] = {}
        self.ble_available = sys.platform == "linux"
        self._usb_handled: set[str] = set()
        self._usb_manual: set[str] = set()
        self._ble_next_allowed: dict[str, float] = {}
        self._ble_force: set[str] = set()

    async def watch(self) -> None:
        await asyncio.gather(self._usb_loop(), self._ble_loop())

    def snapshot(self) -> dict:
        sessions = self.service.sessions
        return {
            "usb_cameras": [
                {"id": f"usb:{c.camera_ip}", "interface": c.interface, "camera_ip": c.camera_ip,
                 "syncing": f"usb:{c.camera_ip}" in sessions}
                for c in self.usb_cameras
            ],
            "ble_available": self.ble_available,
            "ble_seen": self.ble_seen,
        }

    # --- manual control --------------------------------------------------

    def request_sync(self, mode: str, device: str | None) -> int:
        """Queue syncs; the watchers pick them up on their next pass."""
        requested = 0
        sessions = self.service.sessions
        if mode in ("usb", "all"):
            for cam in self.usb_cameras:
                key = f"usb:{cam.camera_ip}"
                if (device is None or device == key) and key not in sessions:
                    self._usb_handled.discard(cam.camera_ip)
                    self._usb_manual.add(cam.camera_ip)
                    requested += 1
        if mode in ("ble", "wifi", "all") and self.ble_available:
            for paired in self.service.settings.cameras:
                key = f"ble:{paired.address}"
                if (device is None or device in (key, paired.address)) and key not in sessions:
                    self._ble_force.add(paired.address)
                    requested += 1
        return requested

    async def scan_ble(self) -> list[dict]:
        if not self.ble_available:
            raise RuntimeError("Bluetooth is only supported on Linux hosts")
        async with self.service.bluetooth_lock:
            adverts = await ble.scan(timeout=BLE_SCAN_SECONDS)
        paired = {c.address for c in self.service.settings.cameras}
        return [{**a.__dict__, "paired": a.address in paired} for a in adverts]

    async def pair(self, address: str, name: str) -> None:
        if self.pairing:
            raise RuntimeError("another camera is being paired")
        self.pairing = address
        self.service.publish_status()
        camera = ble.GoProBle(address)
        try:
            async with self.service.bluetooth_lock:
                await camera.connect(pair=True)
                ssid, _ = await camera.wifi_credentials()
            self.service.log("success", f"paired with {name or address} (Wi-Fi {ssid})")
        finally:
            await camera.disconnect()
            self.pairing = None
            self.service.publish_status()
        settings = self.service.settings
        settings.cameras = [c for c in settings.cameras if c.address != address]
        settings.cameras.append(PairedCamera(address=address, name=name))
        self.service.settings_store.save(settings)

    def forget(self, address: str) -> None:
        settings = self.service.settings
        settings.cameras = [c for c in settings.cameras if c.address != address]
        self.service.settings_store.save(settings)

    def session_finished(self, session: Session, ok: bool) -> None:
        if session.mode == "wifi":
            cooldown = self.service.settings.ble_cooldown_minutes * 60 if ok else BLE_FAILURE_COOLDOWN
            self._ble_next_allowed[session.source] = time.time() + cooldown

    # --- watchers --------------------------------------------------------

    async def _usb_loop(self) -> None:
        service = self.service
        while True:
            try:
                settings = service.settings
                cameras = await usb.discover(service.run_dir)
                changed = {c.camera_ip for c in cameras} != {c.camera_ip for c in self.usb_cameras}
                self.usb_cameras = cameras
                present = {c.camera_ip for c in cameras}
                # Forget cameras that were unplugged, so plugging in again triggers a sync.
                self._usb_handled &= present
                self._usb_manual &= present
                for cam in cameras:
                    key = f"usb:{cam.camera_ip}"
                    if cam.camera_ip in self._usb_handled or key in service.sessions:
                        continue
                    if not (settings.usb_enabled or cam.camera_ip in self._usb_manual):
                        continue
                    if not service.has_capacity(settings):
                        break
                    self._usb_handled.add(cam.camera_ip)
                    self._usb_manual.discard(cam.camera_ip)
                    service.start_session(Session(key, "usb", cam.camera_ip, f"USB {cam.interface}", provider=self.kind),
                                          self._offload_usb(cam))
                    changed = True
                if changed:
                    service.publish_status()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                service.log("error", f"USB watcher: {exc}")
            await asyncio.sleep(USB_POLL_SECONDS)

    async def _ble_loop(self) -> None:
        service = self.service
        if not self.ble_available:
            service.log("info", "Bluetooth watcher disabled (Linux only); USB offload still works")
            return
        while True:
            settings = service.settings
            wanted = settings.ble_enabled or self._ble_force
            if not (wanted and settings.cameras) or self.pairing:
                await asyncio.sleep(5)
                continue
            try:
                async with service.bluetooth_lock:
                    adverts = await ble.scan(timeout=BLE_SCAN_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                service.log("error", f"Bluetooth scan failed: {exc}")
                await asyncio.sleep(30)
                continue
            paired = {c.address: c for c in settings.cameras}
            now = time.time()
            for adv in adverts:
                cam = paired.get(adv.address)
                if cam is None:
                    continue
                self.ble_seen[adv.address] = {"ts": now, "rssi": adv.rssi, "powered_on": adv.powered_on}
                key = f"ble:{adv.address}"
                forced = adv.address in self._ble_force
                if key in service.sessions or not (settings.ble_enabled or forced):
                    continue
                # Only react to a camera the user switched on; a sleeping camera keeps advertising.
                if adv.powered_on is False and not forced:
                    continue
                if now < self._ble_next_allowed.get(adv.address, 0) and not forced:
                    continue
                radio = next((r for r in settings.wifi_interfaces if r not in service.radios_in_use), None)
                if radio is None or not service.has_capacity(settings):
                    continue  # Waits for a radio or a slot to free up.
                self._ble_force.discard(adv.address)
                service.radios_in_use.add(radio)
                service.start_session(Session(key, "wifi", adv.address, cam.name or adv.name or "GoPro",
                                              radio=radio, provider=self.kind),
                                      self._offload_wifi(cam, radio))
            service.publish_status()
            await asyncio.sleep(2)

    # --- offload flows ---------------------------------------------------

    def _identify(self, session: Session, info: dict, fallback: str) -> tuple[str, str]:
        return self.service.identify(session, info.get("serial_number") or fallback, info.get("model_name") or "GoPro")

    def _offload_usb(self, cam: usb.UsbCamera) -> Flow:
        async def flow(session: Session):
            service = self.service
            settings = service.settings
            service.log("info", f"camera detected on USB ({cam.interface}, {cam.camera_ip})")
            http = GoProHttp(cam.camera_ip)
            try:
                await http.wait_ready()
                await http.set_wired_control(True)
                camera_id, label = self._identify(session, await http.info(), cam.camera_ip)
                session.battery = await http.battery_percent()
                result = await service.engine(settings, session).run(http, camera_id, label, keep_alive=http.keep_alive)
                return session.camera, result
            finally:
                try:
                    await http.set_wired_control(False)
                except Exception:
                    pass
                await http.close()

        return flow

    def _offload_wifi(self, paired: PairedCamera, radio: str) -> Flow:
        async def flow(session: Session):
            service = self.service
            settings: Settings = service.settings
            wifi = WifiLink(radio, service.run_dir)
            # Check the radio before waking anything up.
            await wifi.ensure_safe()
            service.log("info", f"{session.name} is on; connecting over Bluetooth")
            camera = ble.GoProBle(paired.address)
            async with service.bluetooth_lock:
                await camera.connect()
            http: GoProHttp | None = None
            try:
                await camera.set_wifi_ap(True)
                ssid, password = await camera.wifi_credentials()
                service.log("info", f"{session.name}: joining camera Wi-Fi '{ssid}' on {radio}")
                await wifi.connect(ssid, password)
                http = GoProHttp(WIFI_AP_HOST, interface=radio)
                await http.wait_ready()
                camera_id, label = self._identify(session, await http.info(), paired.address)
                session.battery = await http.battery_percent()
                if session.battery is not None and session.battery < LOW_BATTERY:
                    service.log("warning", f"{session.camera}: battery at {session.battery}%; the offload may stop early")
                result = await service.engine(settings, session).run(http, camera_id, label, keep_alive=camera.keep_alive)
                return session.camera, result
            finally:
                if http is not None:
                    await http.close()
                # Safe even if joining failed half-way: only touches our own supplicant and 10.5.5.x.
                await wifi.disconnect()
                try:
                    if settings.sleep_after_sync:
                        await camera.sleep()
                    else:
                        await camera.set_wifi_ap(False)
                except Exception:
                    pass
                await camera.disconnect()

        return flow
