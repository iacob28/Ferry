"""Orchestration: watch for cameras over USB and BLE, run offloads in parallel, publish events."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import ble, usb
from .camera import WIFI_AP_HOST, GoProHttp
from .config import PairedCamera, Settings, SettingsStore
from .engine import Progress, RunResult, SyncEngine
from .store import Store
from .targets import make_target
from .wifi import WifiLink

log = logging.getLogger("gopro_sync")

USB_POLL_SECONDS = 3
BLE_SCAN_SECONDS = 6
# Back-off after a failed wireless attempt, so a flaky camera isn't hammered.
BLE_FAILURE_COOLDOWN = 5 * 60
LOW_BATTERY = 15


class EventBus:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self.history: deque[dict] = deque(maxlen=300)

    def publish(self, event: dict) -> None:
        if event["type"] == "log":
            self.history.append(event)
        for queue in list(self._subscribers):
            if queue.qsize() < 500:
                queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)


@dataclass
class Session:
    """One offload of one camera."""

    id: str
    mode: str  # "usb" | "wifi"
    source: str  # USB camera IP or BLE address
    name: str
    state: str = "connecting"
    camera: str | None = None
    battery: int | None = None
    radio: str | None = None
    progress: Progress | None = None
    started_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = None

    def snapshot(self) -> dict:
        return {
            "id": self.id, "mode": self.mode, "name": self.name, "state": self.state, "camera": self.camera,
            "battery": self.battery, "radio": self.radio, "started_at": self.started_at,
            "progress": self.progress.snapshot() if self.progress else None,
        }


Flow = Callable[[Session], Awaitable[tuple[str, RunResult]]]


class SyncService:
    def __init__(self, settings_store: SettingsStore, store: Store, data_dir: Path):
        self.settings_store = settings_store
        self.store = store
        self.data_dir = data_dir
        self.run_dir = data_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.staging = data_dir / "staging"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.thumbs = data_dir / "thumbs"
        self.events = EventBus()

        self.sessions: dict[str, Session] = {}
        self.pairing: str | None = None
        self.usb_cameras: list[usb.UsbCamera] = []
        self.ble_seen: dict[str, dict] = {}
        self.ble_available = sys.platform == "linux"
        self._radios_in_use: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self._usb_handled: set[str] = set()
        self._usb_manual: set[str] = set()
        self._ble_next_allowed: dict[str, float] = {}
        self._ble_force: set[str] = set()
        self._ble_lock = asyncio.Lock()

    @property
    def settings(self) -> Settings:
        return self.settings_store.load()

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._usb_loop()), asyncio.create_task(self._ble_loop())]
        self.log("info", "service started")

    async def stop(self) -> None:
        tasks = [*self._tasks, *[s.task for s in self.sessions.values() if s.task]]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # --- events ----------------------------------------------------------

    def log(self, level: str, message: str) -> None:
        getattr(log, level if level != "success" else "info")(message)
        self.events.publish({"type": "log", "ts": time.time(), "level": level, "message": message})

    def _publish_status(self) -> None:
        self.events.publish({"type": "status", "status": self.snapshot()})

    @property
    def state(self) -> str:
        if self.sessions:
            return "syncing"
        return "pairing" if self.pairing else "idle"

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "sessions": [s.snapshot() for s in self.sessions.values()],
            "pairing": self.pairing,
            "usb_cameras": [
                {"id": f"usb:{c.camera_ip}", "interface": c.interface, "camera_ip": c.camera_ip,
                 "syncing": f"usb:{c.camera_ip}" in self.sessions}
                for c in self.usb_cameras
            ],
            "ble_available": self.ble_available,
            "ble_seen": self.ble_seen,
            "radios": {"configured": self.settings.wifi_interfaces, "in_use": sorted(self._radios_in_use)},
            "totals": self.store.totals(),
        }

    # --- manual control --------------------------------------------------

    def request_sync(self, mode: str = "all", camera: str | None = None) -> str:
        """Queue syncs; the watchers pick them up on their next pass."""
        requested = 0
        if mode in ("usb", "all"):
            for cam in self.usb_cameras:
                key = f"usb:{cam.camera_ip}"
                if (camera is None or camera == key) and key not in self.sessions:
                    self._usb_handled.discard(cam.camera_ip)
                    self._usb_manual.add(cam.camera_ip)
                    requested += 1
        if mode in ("ble", "wifi", "all") and self.ble_available:
            for paired in self.settings.cameras:
                key = f"ble:{paired.address}"
                if (camera is None or camera in (key, paired.address)) and key not in self.sessions:
                    self._ble_force.add(paired.address)
                    requested += 1
        if not requested:
            return "nothing to sync: no idle camera is connected or paired"
        return f"sync requested for {requested} camera(s)"

    def cancel(self, session_id: str | None = None) -> int:
        cancelled = 0
        for session in list(self.sessions.values()):
            if (session_id is None or session.id == session_id) and session.task and not session.task.done():
                session.task.cancel()
                cancelled += 1
        return cancelled

    async def scan_ble(self) -> list[dict]:
        if not self.ble_available:
            raise RuntimeError("Bluetooth is only supported on Linux hosts")
        async with self._ble_lock:
            adverts = await ble.scan(timeout=BLE_SCAN_SECONDS)
        paired = {c.address for c in self.settings.cameras}
        return [{**a.__dict__, "paired": a.address in paired} for a in adverts]

    async def pair(self, address: str, name: str) -> None:
        if self.pairing:
            raise RuntimeError("another camera is being paired")
        self.pairing = address
        self._publish_status()
        camera = ble.GoProBle(address)
        try:
            async with self._ble_lock:
                await camera.connect(pair=True)
                ssid, _ = await camera.wifi_credentials()
            self.log("success", f"paired with {name or address} (Wi-Fi {ssid})")
        finally:
            await camera.disconnect()
            self.pairing = None
            self._publish_status()
        settings = self.settings
        settings.cameras = [c for c in settings.cameras if c.address != address]
        settings.cameras.append(PairedCamera(address=address, name=name))
        self.settings_store.save(settings)

    def forget(self, address: str) -> None:
        settings = self.settings
        settings.cameras = [c for c in settings.cameras if c.address != address]
        self.settings_store.save(settings)

    # --- watchers --------------------------------------------------------

    def _has_capacity(self, settings: Settings) -> bool:
        return len(self.sessions) < max(1, settings.max_parallel_syncs)

    async def _usb_loop(self) -> None:
        while True:
            try:
                settings = self.settings
                cameras = await usb.discover(self.run_dir)
                changed = {c.camera_ip for c in cameras} != {c.camera_ip for c in self.usb_cameras}
                self.usb_cameras = cameras
                present = {c.camera_ip for c in cameras}
                # Forget cameras that were unplugged, so plugging in again triggers a sync.
                self._usb_handled &= present
                self._usb_manual &= present
                for cam in cameras:
                    key = f"usb:{cam.camera_ip}"
                    if cam.camera_ip in self._usb_handled or key in self.sessions:
                        continue
                    if not (settings.usb_enabled or cam.camera_ip in self._usb_manual):
                        continue
                    if not self._has_capacity(settings):
                        break
                    self._usb_handled.add(cam.camera_ip)
                    self._usb_manual.discard(cam.camera_ip)
                    self._start(Session(key, "usb", cam.camera_ip, f"USB {cam.interface}"),
                                self._offload_usb(cam))
                    changed = True
                if changed:
                    self._publish_status()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log("error", f"USB watcher: {exc}")
            await asyncio.sleep(USB_POLL_SECONDS)

    async def _ble_loop(self) -> None:
        if not self.ble_available:
            self.log("info", "Bluetooth watcher disabled (Linux only); USB offload still works")
            return
        while True:
            settings = self.settings
            wanted = settings.ble_enabled or self._ble_force
            if not (wanted and settings.cameras) or self.pairing:
                await asyncio.sleep(5)
                continue
            try:
                async with self._ble_lock:
                    adverts = await ble.scan(timeout=BLE_SCAN_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log("error", f"Bluetooth scan failed: {exc}")
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
                if key in self.sessions or not (settings.ble_enabled or forced):
                    continue
                # Only react to a camera the user switched on; a sleeping camera keeps advertising.
                if adv.powered_on is False and not forced:
                    continue
                if now < self._ble_next_allowed.get(adv.address, 0) and not forced:
                    continue
                radio = next((r for r in settings.wifi_interfaces if r not in self._radios_in_use), None)
                if radio is None or not self._has_capacity(settings):
                    continue  # Waits for a radio or a slot to free up.
                self._ble_force.discard(adv.address)
                self._radios_in_use.add(radio)
                self._start(Session(key, "wifi", adv.address, cam.name or adv.name or "GoPro", radio=radio),
                            self._offload_wifi(cam, radio))
            self._publish_status()
            await asyncio.sleep(2)

    # --- sessions --------------------------------------------------------

    def _start(self, session: Session, flow: Flow) -> None:
        self.sessions[session.id] = session
        session.task = asyncio.create_task(self._run_session(session, flow))
        self._publish_status()

    async def _run_session(self, session: Session, flow: Flow) -> None:
        run_id = self.store.start_run(session.mode)
        ok = False
        try:
            camera, result = await flow(session)
        except asyncio.CancelledError:
            self.store.finish_run(run_id, "cancelled", 0, 0, camera=session.camera)
            self.log("warning", f"{session.camera or session.name}: sync cancelled")
        except Exception as exc:
            self.store.finish_run(run_id, "failed", 0, 0, str(exc), camera=session.camera)
            self.log("error", f"{session.camera or session.name}: sync failed: {exc}")
        else:
            ok = True
            self.store.finish_run(run_id, "ok", result.files, result.bytes, camera=camera)
            self.log("success", f"{camera}: {result.files} files, {result.bytes / 1e9:.2f} GB offloaded"
                                + (f", {result.deleted} deleted from camera" if result.deleted else ""))
        finally:
            if session.mode == "wifi":
                if session.radio:
                    self._radios_in_use.discard(session.radio)
                cooldown = self.settings.ble_cooldown_minutes * 60 if ok else BLE_FAILURE_COOLDOWN
                self._ble_next_allowed[session.source] = time.time() + cooldown
            self.sessions.pop(session.id, None)
            self._publish_status()

    def _engine(self, settings: Settings, session: Session) -> SyncEngine:
        def on_progress(progress: Progress) -> None:
            session.progress = progress
            self.events.publish({"type": "progress", "session": session.id, "progress": progress.snapshot()})

        return SyncEngine(self.store, settings, make_target(settings), self.staging,
                          lambda level, msg: self.log(level, f"{session.camera or session.name}: {msg}"), on_progress,
                          self.thumbs)

    def _identify(self, session: Session, info: dict, fallback: str) -> tuple[str, str]:
        camera_id = info.get("serial_number") or fallback
        label = info.get("model_name") or "GoPro"
        session.camera = f"{label} {camera_id}"
        session.state = "syncing"
        self._publish_status()
        return camera_id, label

    def _offload_usb(self, cam: usb.UsbCamera) -> Flow:
        async def flow(session: Session):
            settings = self.settings
            self.log("info", f"camera detected on USB ({cam.interface}, {cam.camera_ip})")
            http = GoProHttp(cam.camera_ip)
            try:
                await http.wait_ready()
                await http.set_wired_control(True)
                camera_id, label = self._identify(session, await http.info(), cam.camera_ip)
                session.battery = await http.battery_percent()
                result = await self._engine(settings, session).run(http, camera_id, label, keep_alive=http.keep_alive)
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
            settings = self.settings
            wifi = WifiLink(radio, self.run_dir)
            # Check the radio before waking anything up.
            await wifi.ensure_safe()
            self.log("info", f"{session.name} is on; connecting over Bluetooth")
            camera = ble.GoProBle(paired.address)
            async with self._ble_lock:
                await camera.connect()
            http: GoProHttp | None = None
            try:
                await camera.set_wifi_ap(True)
                ssid, password = await camera.wifi_credentials()
                self.log("info", f"{session.name}: joining camera Wi-Fi '{ssid}' on {radio}")
                await wifi.connect(ssid, password)
                http = GoProHttp(WIFI_AP_HOST, interface=radio)
                await http.wait_ready()
                camera_id, label = self._identify(session, await http.info(), paired.address)
                session.battery = await http.battery_percent()
                if session.battery is not None and session.battery < LOW_BATTERY:
                    self.log("warning", f"{session.camera}: battery at {session.battery}%; the offload may stop early")
                result = await self._engine(settings, session).run(http, camera_id, label, keep_alive=camera.keep_alive)
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
