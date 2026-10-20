"""Persistent settings, stored as JSON in the data directory.

Destinations and credentials are defined once and referenced by id. Sync rules are layered:
global defaults, then per provider, then per device; each layer only overrides what it sets.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

VERSION = 2
# Placeholders available in a destination path and in the folder layout.
TEMPLATE_FIELDS = ("year", "month", "day", "date", "camera", "provider", "device", "model")
# Keys holding secrets, wherever they appear (credentials, device config); masked by the API.
SECRET_KEYS = frozenset({"password", "secret", "refresh_token", "access_code"})
POLICY_FIELDS = ("destination", "credentials", "path", "folder_template", "delete_after_sync")
# Keys only GoPro Sync (version 1) settings have.
V1_KEYS = frozenset({"target", "smb", "local_path", "cameras", "usb_enabled", "ble_enabled", "folder_template",
                     "delete_after_sync", "sleep_after_sync", "ble_cooldown_minutes", "wifi_interface"})


def env(name: str, default: str | None = None) -> str | None:
    """FERRY_<name>, falling back to the GOPRO_SYNC_<name> variables of earlier releases."""
    return os.environ.get(f"FERRY_{name}") or os.environ.get(f"GOPRO_SYNC_{name}") or default


def data_dir() -> Path:
    legacy = Path.home() / ".gopro-sync"
    default = legacy if legacy.exists() and not (Path.home() / ".ferry").exists() else Path.home() / ".ferry"
    path = Path(env("DATA") or default)
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- templates -------------------------------------------------------------------

_SAMPLE = {"year": "2026", "month": "09", "day": "26", "date": "2026-09-26", "camera": "HERO9 Black",
           "provider": "GoPro", "device": "HERO9", "model": "HERO9 Black"}


def _segments(text: str) -> list[str]:
    parts = [p for p in text.replace("\\", "/").split("/") if p and p != "."]
    if ".." in parts:
        raise ValueError("paths may not contain '..'")
    return parts


def check_template(template: str) -> str:
    try:
        _segments(template.format_map(_SAMPLE))
    except KeyError as exc:
        raise ValueError(f"unknown placeholder {{{exc.args[0]}}}; available: "
                         + " ".join(f"{{{f}}}" for f in TEMPLATE_FIELDS)) from None
    except (IndexError, ValueError) as exc:
        raise ValueError(f"invalid template {template!r}: {exc}") from None
    return template


def template_fields(created: int, provider: str, device: str, model: str) -> dict[str, str]:
    # Device clocks have no timezone, so the epoch is read back as naive wall time.
    taken = datetime.fromtimestamp(created, tz=timezone.utc)

    def clean(value: str) -> str:
        return value.replace("/", "-").replace("\\", "-").strip() or "unknown"

    return {"year": f"{taken:%Y}", "month": f"{taken:%m}", "day": f"{taken:%d}", "date": f"{taken:%Y-%m-%d}",
            "camera": clean(model), "model": clean(model), "provider": clean(provider), "device": clean(device)}


def render_key(path: str, folder_template: str, fields: dict[str, str], name: str) -> str:
    """Destination key: <path>/<folder layout>/<name>, as a relative POSIX path."""
    parts = [*_segments(path.format_map(fields)), *_segments(folder_template.format_map(fields))]
    return "/".join([*parts, name])


# --- models ----------------------------------------------------------------------


class Credential(BaseModel):
    id: str
    name: str = ""
    kind: Literal["password", "access_key", "oauth_google"] = "password"
    username: str = ""
    password: str = ""
    # access_key (S3 / R2)
    key_id: str = ""
    secret: str = ""
    # oauth_google
    refresh_token: str = ""
    account: str = ""


class DestinationConfig(BaseModel):
    id: str
    name: str = ""
    type: str = "smb"
    # smb
    host: str = ""
    share: str = ""
    # smb: folder inside the share; s3: key prefix; gdrive: root folder
    base_path: str = ""
    # local
    path: str = ""
    # s3 / r2
    endpoint: str = ""
    region: str = ""
    bucket: str = ""
    credentials: str | None = None


def _check_optional_template(value: str | None) -> str | None:
    return value if value is None else check_template(value)


class SyncOverrides(BaseModel):
    """One layer of sync rules; None means 'inherit from the layer below'."""

    destination: str | None = None
    # Credentials to use instead of the destination's own (same server, different user).
    credentials: str | None = None
    # Folder inside the destination; may use placeholders.
    path: str | None = None
    folder_template: str | None = None
    delete_after_sync: bool | None = None

    _check = field_validator("path", "folder_template")(_check_optional_template)


class SyncDefaults(BaseModel):
    destination: str = ""
    path: str = "{provider}"
    folder_template: str = "{year}/{date}"
    delete_after_sync: bool = False

    _check = field_validator("path", "folder_template")(check_template)


class ProviderSettings(BaseModel):
    enabled: bool = True
    sync: SyncOverrides = Field(default_factory=SyncOverrides)
    # Provider-specific; each provider validates it with its own options model.
    options: dict = Field(default_factory=dict)


class Device(BaseModel):
    id: str
    provider: str
    name: str = ""
    enabled: bool = True
    # Provider-specific (BLE address, serial, host, access code, ...).
    config: dict = Field(default_factory=dict)
    sync: SyncOverrides = Field(default_factory=SyncOverrides)


@dataclass
class SyncPolicy:
    destination: str
    credentials: str | None
    path: str
    folder_template: str
    delete_after_sync: bool
    # Which layer each value came from: "global" | "provider" | "device".
    origin: dict[str, str] = field(default_factory=dict)

    def key(self, fields: dict[str, str], name: str) -> str:
        return render_key(self.path, self.folder_template, fields, name)


class Settings(BaseModel):
    version: int = VERSION
    destinations: list[DestinationConfig] = Field(default_factory=list)
    credentials: list[Credential] = Field(default_factory=list)
    defaults: SyncDefaults = Field(default_factory=SyncDefaults)
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    devices: list[Device] = Field(default_factory=list)
    # Radios used to join camera access points, one device per radio at a time.
    # None of them may carry the host's LAN connection.
    wifi_interfaces: list[str] = Field(default_factory=lambda: ["wlan0"])
    # Upper bound on simultaneous offloads, all providers together.
    max_parallel_syncs: int = 4

    @model_validator(mode="before")
    @classmethod
    def _upgrade(cls, data):
        if isinstance(data, dict) and "version" not in data and V1_KEYS & data.keys():
            return migrate_v1(data)
        return data

    @model_validator(mode="after")
    def _check_references(self):
        for kind, items in (("destination", self.destinations), ("credential", self.credentials),
                            ("device", self.devices)):
            ids = [i.id for i in items]
            dupes = {i for i in ids if ids.count(i) > 1}
            if dupes:
                raise ValueError(f"duplicate {kind} id: {', '.join(sorted(dupes))}")
        dests = {d.id for d in self.destinations}
        creds = {c.id for c in self.credentials}
        layers = [("defaults", self.defaults.destination, None)]
        layers += [(f"provider {k}", p.sync.destination, p.sync.credentials) for k, p in self.providers.items()]
        layers += [(f"device {d.id}", d.sync.destination, d.sync.credentials) for d in self.devices]
        for where, dest, cred in layers:
            if dest and dest not in dests:
                raise ValueError(f"{where}: unknown destination {dest!r}")
            if cred and cred not in creds:
                raise ValueError(f"{where}: unknown credentials {cred!r}")
        for d in self.destinations:
            if d.credentials and d.credentials not in creds:
                raise ValueError(f"destination {d.id}: unknown credentials {d.credentials!r}")
        return self

    # --- lookups ---------------------------------------------------------

    def destination(self, dest_id: str | None) -> DestinationConfig | None:
        return next((d for d in self.destinations if d.id == dest_id), None)

    def credential(self, cred_id: str | None) -> Credential | None:
        return next((c for c in self.credentials if c.id == cred_id), None)

    def device(self, device_id: str | None) -> Device | None:
        return next((d for d in self.devices if d.id == device_id), None)

    def provider(self, kind: str) -> ProviderSettings:
        return self.providers.get(kind) or ProviderSettings()

    def devices_of(self, kind: str) -> list[Device]:
        return [d for d in self.devices if d.provider == kind]

    def effective(self, provider: str, device_id: str | None = None) -> SyncPolicy:
        values: dict = {**self.defaults.model_dump(), "credentials": None}
        origin = dict.fromkeys(POLICY_FIELDS, "global")
        layers = [("provider", self.provider(provider).sync)]
        device = self.device(device_id)
        if device is not None:
            layers.append(("device", device.sync))
        for name, layer in layers:
            for key, value in layer.model_dump().items():
                if value is not None:
                    values[key] = value
                    origin[key] = name
        return SyncPolicy(**values, origin=origin)


def migrate_v1(data: dict) -> dict:
    """GoPro Sync settings (a single SMB/local target and paired cameras) to version 2."""
    smb = data.get("smb") or {}
    destinations, credentials = [], []
    if smb.get("host"):
        cred = None
        if smb.get("username"):
            cred = "nas"
            credentials.append({"id": cred, "name": smb["username"], "kind": "password",
                                "username": smb["username"], "password": smb.get("password", "")})
        destinations.append({"id": "nas", "name": smb["host"], "type": "smb", "host": smb["host"],
                             "share": smb.get("share", ""), "base_path": smb.get("base_path", ""), "credentials": cred})
    if data.get("local_path"):
        destinations.append({"id": "local", "name": data["local_path"], "type": "local", "path": data["local_path"]})
    wanted = "local" if data.get("target") == "local" else "nas"
    default_dest = wanted if any(d["id"] == wanted for d in destinations) else (destinations[0]["id"] if destinations else "")
    radios = data.get("wifi_interfaces")
    if radios is None:
        legacy = data.get("wifi_interface")
        radios = [legacy] if legacy else ["wlan0"]
    return {
        "version": VERSION,
        "destinations": destinations,
        "credentials": credentials,
        # Empty path: files keep landing exactly where GoPro Sync put them.
        "defaults": {"destination": default_dest, "path": "",
                     "folder_template": data.get("folder_template", "{year}/{date}"),
                     "delete_after_sync": data.get("delete_after_sync", False)},
        "providers": {"gopro": {"options": {
            "usb": data.get("usb_enabled", True), "ble": data.get("ble_enabled", True),
            "sleep_after_sync": data.get("sleep_after_sync", True),
            "cooldown_minutes": data.get("ble_cooldown_minutes", 30)}}},
        "devices": [{"id": f"gopro:{c['address']}", "provider": "gopro", "name": c.get("name", ""),
                     "config": {"address": c["address"]}} for c in data.get("cameras", [])],
        "wifi_interfaces": radios,
        "max_parallel_syncs": data.get("max_parallel_syncs", 4),
    }


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> Settings:
        with self._lock:
            if not self.path.exists():
                return Settings()
            raw = json.loads(self.path.read_text())
            settings = Settings.model_validate(raw)
            if raw.get("version", 1) < VERSION and raw:
                # Keep the old file once, then store the upgraded one.
                backup = self.path.with_name(self.path.name + ".v1.bak")
                if not backup.exists():
                    shutil.copy2(self.path, backup)
                self.save(settings)
            return settings

    def save(self, settings: Settings) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(settings.model_dump(), indent=2))
            # The file holds passwords and access codes.
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def update(self, change: Callable[[Settings], None]) -> Settings:
        """Read-modify-write under the lock (watchers and the API both write)."""
        with self._lock:
            settings = self.load()
            change(settings)
            settings = Settings.model_validate(settings.model_dump())
            self.save(settings)
            return settings
