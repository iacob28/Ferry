"""Drones and cameras that show up as USB storage (DJI, Insta360, card readers, ...)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from ...core.engine import DeviceInfo
from ...core.session import Flow, Session
from ...core.settings import Device, Settings
from ..base import Provider
from . import detect
from .profiles import DEFAULT_EXTENSIONS, GENERIC, Profile, gopro_card_info, identify_volume
from .source import FolderSource

POLL_SECONDS = 3
# USB product strings that say nothing about the device.
GENERIC_PRODUCTS = {"", "mass storage", "usb mass storage", "usb storage", "storage", "card reader", "sd card reader"}


class UsbStorageOptions(BaseModel):
    auto: bool = True  # sync an adopted device as soon as it is plugged in
    # Sync unknown devices without asking first.
    auto_adopt: bool = False
    # Folders where the host already mounts drives (e.g. /media); each sub-folder with DCIM counts.
    watch_folders: list[str] = Field(default_factory=list)
    # Also watch the host's built-in SD slot (mmcblk). Off by default: Raspberry Pis that boot
    # from SD keep the system there. Needs the "b 179:* rmw" device rule in compose.
    sd_slot: bool = False
    extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_EXTENSIONS))


@dataclass
class Volume:
    candidate: detect.Candidate
    root: Path | None = None  # where its files are (our mount point, or the folder itself)
    profile: Profile = GENERIC
    # "checking" | "ready" | "not-media" | "done" (synced and unmounted: safe to unplug) | "error"
    state: str = "checking"
    error: str = ""
    folders: list[str] = field(default_factory=list)
    # What GoPro writes to its cards (MISC/version.txt): serial and model.
    card: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.candidate.id

    @property
    def model(self) -> str:
        if self.card.get("model"):
            return self.card["model"]
        product = self.candidate.product.strip()
        if product.lower() in GENERIC_PRODUCTS:
            return self.profile.label if self.profile is not GENERIC else (self.candidate.label or "USB drive")
        return product if product.lower().startswith(self.profile.label.lower()) else f"{self.profile.label} {product}".strip()


@dataclass
class Target:
    """Whose files a volume holds, and so whose rules and "already synced" keys apply."""

    rules: str  # provider kind: "usbstorage", or "gopro" / "bambu" for their SD cards
    device: Device | None  # the device whose rules apply, if any
    store_id: str  # device id in the store: the camera's serial for a GoPro card, so nothing syncs twice
    model: str
    adopt: bool  # unknown: waits for the user to adopt it


class UsbStorageProvider(Provider):
    kind = "usbstorage"
    label = "USB drones & cameras"
    icon = "ph-drone"
    options_model = UsbStorageOptions

    def __init__(self, service):
        super().__init__(service)
        self.volumes: dict[str, Volume] = {}
        self._handled: set[str] = set()
        self._manual: set[str] = set()
        self.mount_root = service.run_dir / "mnt"

    # --- state -----------------------------------------------------------

    def snapshot(self) -> dict:
        settings = self.service.settings
        volumes = []
        for v in self.volumes.values():
            if v.state == "not-media":
                continue
            target = self.target(settings, v)
            volumes.append({
                "id": v.id, "state": v.state, "error": v.error, "model": v.model, "profile": v.profile.key,
                "profile_label": v.profile.label, "icon": v.profile.icon, "label": v.candidate.label,
                "kind": v.candidate.kind, "source": v.candidate.source, "rules": target.rules,
                "device": target.device.id if target.device else None, "adopt": target.adopt,
                "syncing": self._session_id(v) in self.service.sessions})
        return {"usb_volumes": volumes}

    @staticmethod
    def _session_id(volume: Volume) -> str:
        return f"usbstorage:{volume.id}"

    def _mount_point(self, volume: Volume) -> Path:
        return self.mount_root / volume.id.replace(":", "_").replace("/", "_")

    def device_for(self, settings: Settings, volume_id: str) -> Device | None:
        return next((d for d in settings.devices_of(self.kind) if d.config.get("volume") == volume_id), None)

    def target(self, settings: Settings, volume: Volume) -> Target:
        """GoPro cards sync as their camera, a Bambu card as the printer; anything else as itself."""
        profile = volume.profile
        if profile.rules == "gopro" and volume.card.get("serial"):
            serial = volume.card["serial"]
            camera = next((d for d in settings.devices_of("gopro") if d.config.get("serial") == serial), None)
            return Target("gopro", camera, serial, volume.model, adopt=False)
        if profile.rules == "bambu":
            printers = settings.devices_of("bambu")
            if len(printers) == 1:  # The card can only be that printer's; with several, ask.
                printer = printers[0]
                return Target("bambu", printer, printer.id, printer.config.get("model") or "Bambu Lab", adopt=False)
        own = self.device_for(settings, volume.id)
        return Target(profile.rules if profile.rules != "gopro" else "usbstorage", own, volume.id, volume.model,
                      adopt=own is None)

    # --- manual control --------------------------------------------------

    def request_sync(self, mode: str, device: str | None) -> int:
        if mode not in ("usb", "all", self.kind):
            return 0
        settings = self.service.settings
        requested = 0
        for volume in self.volumes.values():
            target = self.target(settings, volume)
            dev = target.device
            matches = device is None or device in (volume.id, self._session_id(volume), dev.id if dev else None)
            if matches and volume.state in ("ready", "done", "error") and self._session_id(volume) not in self.service.sessions:
                if target.adopt and device is None:
                    continue  # "Sync all" leaves unknown drives alone; syncing one by id adopts it.
                self._handled.discard(volume.id)
                self._manual.add(volume.id)
                requested += 1
        return requested

    async def new_device(self, name: str, config: dict) -> Device:
        volume = self.volumes.get(config.get("volume", ""))
        if volume is None or volume.state == "not-media":
            raise ValueError("that drive is not connected; plug it in first")
        # Adopting opens its settings; it syncs when the user presses Sync (or on the next plug-in).
        self._handled.add(volume.id)
        return self._device(volume, name)

    def _device(self, volume: Volume, name: str = "") -> Device:
        c = volume.candidate
        return Device(id=volume.id, provider=self.kind, name=name or volume.model, config={
            "volume": volume.id, "model": volume.model, "profile": volume.profile.key, "label": c.label,
            "vendor": c.vendor, "serial": c.serial, "icon": volume.profile.icon,
        })

    # --- watcher ---------------------------------------------------------

    async def watch(self) -> None:
        try:
            while True:
                try:
                    await self._poll()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.service.log("error", f"USB storage watcher: {exc}")
                await asyncio.sleep(POLL_SECONDS)
        finally:
            for volume in self.volumes.values():
                if volume.candidate.kind == "block":
                    await detect.unmount(self._mount_point(volume), lazy=True)

    async def _poll(self) -> None:
        service = self.service
        settings = service.settings
        opts = self.options(settings)
        found = {c.id: c for c in [*await detect.block_candidates(opts.sd_slot), *detect.dir_candidates(opts.watch_folders)]}
        changed = False
        for vid in [v for v in self.volumes if v not in found]:
            volume = self.volumes.pop(vid)
            self._handled.discard(vid)
            self._manual.discard(vid)
            if volume.candidate.kind == "block" and self._session_id(volume) not in service.sessions:
                await detect.unmount(self._mount_point(volume), lazy=True)
            if volume.state != "not-media":
                service.log("info", f"{volume.model} removed")
            changed = True
        for vid, candidate in found.items():
            if vid not in self.volumes:
                self.volumes[vid] = volume = Volume(candidate)
                await self._inspect(volume)
                changed = True
        auto = self.auto_sync(settings) and opts.auto
        for volume in self.volumes.values():
            if volume.state not in ("ready", "done", "error") or volume.id in self._handled:
                continue
            if self._session_id(volume) in service.sessions or not service.has_capacity(settings):
                continue
            target = self.target(settings, volume)
            manual = volume.id in self._manual
            if target.adopt and (manual or (auto and opts.auto_adopt)):
                device = self._device(volume)
                service.settings_store.update(lambda s, d=device: s.devices.append(d) if not s.device(d.id) else None)
                service.log("info", f"{device.name} added as a device")
                target = Target(target.rules, device, target.store_id, target.model, adopt=False)
            if target.adopt:
                continue
            # A card follows its camera's or printer's provider switch too.
            wanted = auto and settings.provider(target.rules).enabled and (target.device is None or target.device.enabled)
            if not (manual or wanted):
                continue
            self._handled.add(volume.id)
            self._manual.discard(volume.id)
            name = (target.device.name if target.device else "") or target.model
            service.start_session(Session(self._session_id(volume), "usb", volume.id, name, provider=self.kind,
                                          device=target.device.id if target.device else None, rules=target.rules),
                                  self._offload(volume, opts, target))
            changed = True
        if changed:
            service.publish_status()

    async def _inspect(self, volume: Volume) -> None:
        """Mount (read-only) and look for camera media; anything else is left alone from then on."""
        candidate = volume.candidate
        try:
            if candidate.kind == "block":
                volume.root = self._mount_point(volume)
                await detect.mount(candidate, volume.root)
            else:
                volume.root = Path(candidate.source)
            found = identify_volume(volume.root, candidate.vendor)
            if found is None:
                volume.state = "not-media"
                if candidate.kind == "block":
                    await detect.unmount(volume.root)
                return
            volume.profile, volume.folders = found
            if volume.profile.rules == "gopro":
                volume.card = gopro_card_info(volume.root)
            volume.state = "ready"
            target = self.target(self.service.settings, volume)
            who = (target.device.name if target.device else "") or target.model
            self.service.log("info", f"{who} connected over USB"
                             + ("; adopt it in Devices to sync it" if target.adopt else ""))
        except Exception as exc:
            volume.state, volume.error = "error", str(exc)
            self.service.log("error", f"cannot read USB drive {candidate.source}: {exc}")

    def _offload(self, volume: Volume, opts: UsbStorageOptions, target: Target) -> Flow:
        async def flow(session: Session):
            service = self.service
            candidate = volume.candidate
            root = volume.root or self._mount_point(volume)
            if candidate.kind == "block":
                await detect.mount(candidate, root)  # Unmounted after an earlier sync.
            volume.root, volume.state = root, "ready"

            async def make_writable() -> None:
                if candidate.kind == "block":
                    await detect.remount(root, writable=True)

            profile = volume.profile
            source = FolderSource(root, candidate.fstype, tuple(profile.extensions or opts.extensions), make_writable,
                                  media_dir=profile.media_dir, paths_from=profile.paths_from)
            device = service.settings.device(session.device)
            info = DeviceInfo(target.rules if target.rules != "usbstorage" else self.kind, profile.label,
                              target.store_id, target.model, device.name if device else "")
            service.identify(session, target.store_id.split(":", 1)[-1][:12], target.model)
            session.camera = device.name if device and device.name else target.model
            try:
                result = await service.engine(service.settings, session).run(source, info)
                volume.state, volume.error = "done", ""
                return session.camera, result
            except BaseException as exc:
                volume.state, volume.error = "error", "" if isinstance(exc, asyncio.CancelledError) else str(exc)
                raise
            finally:
                if candidate.kind == "block":
                    # Flush and release it, so it can be unplugged safely.
                    await asyncio.shield(detect.unmount(root))

        return flow
