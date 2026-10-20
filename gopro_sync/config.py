"""Persistent settings, stored as JSON in the data directory."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


def data_dir() -> Path:
    path = Path(os.environ.get("GOPRO_SYNC_DATA", Path.home() / ".gopro-sync"))
    path.mkdir(parents=True, exist_ok=True)
    return path


class SmbSettings(BaseModel):
    host: str = ""
    share: str = ""
    username: str = ""
    password: str = ""
    # Optional sub-folder inside the share, e.g. "Camera/HERO9".
    base_path: str = ""


class PairedCamera(BaseModel):
    address: str
    name: str = ""


class Settings(BaseModel):
    target: Literal["smb", "local"] = "smb"
    smb: SmbSettings = Field(default_factory=SmbSettings)
    local_path: str = ""
    # Placeholders: {year} {month} {day} {date} {camera}
    folder_template: str = "{year}/{date}"
    delete_after_sync: bool = False
    sleep_after_sync: bool = True
    usb_enabled: bool = True
    ble_enabled: bool = True
    # Radios used to join camera access points, one camera per radio at a time.
    # None of them may carry the host's LAN connection.
    wifi_interfaces: list[str] = Field(default_factory=lambda: ["wlan0"])
    # Upper bound on simultaneous offloads (USB and wireless together).
    max_parallel_syncs: int = 4
    # Minimum time between two wireless offloads of the same camera.
    ble_cooldown_minutes: int = 30
    cameras: list[PairedCamera] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _upgrade(cls, data):
        if isinstance(data, dict) and "wifi_interface" in data:
            data = dict(data)
            legacy = data.pop("wifi_interface")
            data.setdefault("wifi_interfaces", [legacy] if legacy else [])
        return data


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> Settings:
        with self._lock:
            if not self.path.exists():
                return Settings()
            return Settings.model_validate_json(self.path.read_text())

    def save(self, settings: Settings) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(settings.model_dump(), indent=2))
            # The file holds the SMB password.
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
