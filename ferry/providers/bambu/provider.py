"""Bambu Lab printers: copy timelapses off the SD card after each print."""

from __future__ import annotations

import asyncio
import time

from pydantic import BaseModel, Field

from ...core.engine import DeviceInfo
from ...core.session import Flow, Session
from ...core.settings import Device, Settings
from ..base import Provider
from . import discovery
from .ftps import PORT, FtpsError, PrinterFiles
from .mqtt import PrinterMonitor
from .source import PrinterSource

TICK_SECONDS = 5


class BambuOptions(BaseModel):
    auto: bool = True  # sync after each print, and on the timer below
    # Also look for new files every N minutes while the printer is idle (0: only after prints).
    poll_minutes: int = 30
    # The printer assembles the timelapse after the print; give it time before copying.
    delay_after_print_seconds: int = 120
    folders: list[str] = Field(default_factory=lambda: ["timelapse"])
    extensions: list[str] = Field(default_factory=lambda: ["mp4", "avi"])


class BambuProvider(Provider):
    kind = "bambu"
    label = "Bambu Lab"
    icon = "ph-cube"
    options_model = BambuOptions
    device_fields = [
        {"key": "host", "label": "printerHost"},
        {"key": "serial", "label": "printerSerial"},
        {"key": "access_code", "label": "accessCode", "secret": True},
    ]

    def __init__(self, service):
        super().__init__(service)
        self.monitors: dict[str, tuple[tuple, PrinterMonitor]] = {}
        self._next_check: dict[str, float] = {}
        self._forced: set[str] = set()
        self._last_result: dict[str, dict] = {}

    def describe(self) -> dict:
        return {**super().describe(), "device_fields": self.device_fields, "manual_add": True}

    def snapshot(self) -> dict:
        printers = {}
        for device in self.service.settings.devices_of(self.kind):
            entry = self.monitors.get(device.id)
            state = entry[1].state.snapshot() if entry else {"connected": False}
            printers[device.id] = {**state, "syncing": self._session_id(device.id) in self.service.sessions,
                                   "next_check": self._next_check.get(device.id), **self._last_result.get(device.id, {})}
        return {"printers": printers}

    @staticmethod
    def _session_id(device_id: str) -> str:
        return f"bambu:{device_id}"

    # --- devices ---------------------------------------------------------

    async def discover(self) -> list[dict]:
        known = {d.config.get("serial") for d in self.service.settings.devices_of(self.kind)}
        return [{**p, "added": p["serial"] in known} for p in await discovery.discover()]

    async def new_device(self, name: str, config: dict) -> Device:
        host = str(config.get("host", "")).strip()
        serial = str(config.get("serial", "")).strip().upper()
        code = str(config.get("access_code", "")).strip()
        if not (host and serial and code):
            raise ValueError("enter the printer's IP address, serial number and LAN access code")
        port = int(config.get("port") or PORT)
        files = PrinterFiles(host, code, port=port)
        try:
            await asyncio.to_thread(files.open)  # Proves the address and code, and pins the certificate.
            fingerprint = files.fingerprint
        except FtpsError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            await asyncio.to_thread(files.close)
        model = str(config.get("model") or "")
        return Device(id=f"bambu:{serial}", provider=self.kind, name=name or config.get("name") or model or "Printer",
                      config={"host": host, "serial": serial, "access_code": code, "model": model,
                              "cert_sha256": fingerprint, **({"port": port} if port != PORT else {})})

    def request_sync(self, mode: str, device: str | None) -> int:
        if mode not in ("all", "lan", self.kind):
            return 0
        requested = 0
        for dev in self.service.settings.devices_of(self.kind):
            if device in (None, dev.id, self._session_id(dev.id)) and self._session_id(dev.id) not in self.service.sessions:
                self._forced.add(dev.id)
                requested += 1
        return requested

    # --- watcher ---------------------------------------------------------

    async def watch(self) -> None:
        try:
            while True:
                try:
                    self._tick()
                except Exception as exc:
                    self.service.log("error", f"Bambu watcher: {exc}")
                await asyncio.sleep(TICK_SECONDS)
        finally:
            for _, monitor in self.monitors.values():
                monitor.stop()

    def _sync_monitors(self, devices: list[Device]) -> None:
        wanted = {d.id: (d.config.get("host"), d.config.get("serial"), d.config.get("access_code")) for d in devices}
        for device_id in list(self.monitors):
            key, monitor = self.monitors[device_id]
            if wanted.get(device_id) != key:
                monitor.stop()
                del self.monitors[device_id]
        for device_id, key in wanted.items():
            if device_id not in self.monitors and all(key):
                monitor = PrinterMonitor(*key, on_change=self.service.publish_status)
                monitor.start()
                self.monitors[device_id] = (key, monitor)

    def _tick(self) -> None:
        service = self.service
        settings = service.settings
        devices = settings.devices_of(self.kind)
        self._sync_monitors(devices)
        opts: BambuOptions = self.options(settings)
        auto = self.auto_sync(settings) and opts.auto
        now = time.time()
        for device in devices:
            if self._session_id(device.id) in service.sessions:
                continue
            state = self.monitors[device.id][1].state if device.id in self.monitors else None
            forced = device.id in self._forced
            if state is not None and state.printing:
                continue  # Never while printing, not even when asked: the card is busy.
            due = forced
            if not due and auto and device.enabled:
                if state is not None and state.ended_at and now - state.ended_at >= opts.delay_after_print_seconds:
                    due = True
                elif opts.poll_minutes > 0 and now >= self._next_check.get(device.id, 0):
                    due = True
            if not due or not service.has_capacity(settings):
                continue
            if state is not None:
                state.ended_at = None
            self._forced.discard(device.id)
            self._next_check[device.id] = now + max(opts.poll_minutes, 1) * 60
            service.start_session(Session(self._session_id(device.id), "lan", device.config.get("host", ""),
                                          device.name or "Printer", provider=self.kind, device=device.id),
                                  self._offload(device.id, opts))

    def _offload(self, device_id: str, opts: BambuOptions) -> Flow:
        async def flow(session: Session):
            service = self.service
            device = service.settings.device(device_id)
            if device is None:
                raise RuntimeError("the printer was removed")
            cfg = device.config
            files = PrinterFiles(cfg["host"], cfg["access_code"], cfg.get("cert_sha256") or None, int(cfg.get("port") or PORT))
            try:
                await asyncio.to_thread(files.open)
            except FtpsError as exc:
                self._last_result[device_id] = {"last_error": str(exc), "last_check": time.time()}
                raise
            try:
                if not cfg.get("cert_sha256") and files.fingerprint:
                    def pin(settings: Settings) -> None:
                        d = settings.device(device_id)
                        if d is not None:
                            d.config["cert_sha256"] = files.fingerprint

                    service.settings_store.update(pin)
                model = cfg.get("model") or "Bambu Lab"
                service.identify(session, cfg.get("serial", ""), model)
                session.camera = device.name or model
                info = DeviceInfo(self.kind, self.label, device_id, model, device.name)
                source = PrinterSource(files, opts.folders, opts.extensions)
                result = await service.engine(service.settings, session).run(source, info)
                self._last_result[device_id] = {"last_error": "", "last_check": time.time()}
                return session.camera, result
            except Exception as exc:
                self._last_result[device_id] = {"last_error": str(exc), "last_check": time.time()}
                raise
            finally:
                await asyncio.to_thread(files.close)

        return flow
