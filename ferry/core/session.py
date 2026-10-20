"""One offload of one device, as tracked by the service."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .engine import Progress, RunResult


@dataclass
class Session:
    id: str
    mode: str  # how the device is reached: "usb" | "wifi" | ...
    source: str  # provider-specific address (USB camera IP, BLE address, ...)
    name: str
    state: str = "connecting"
    camera: str | None = None
    battery: int | None = None
    radio: str | None = None
    progress: Progress | None = None
    started_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    provider: str = ""
    # Id of the device in settings.devices, once known; selects its sync rules.
    device: str | None = None

    def snapshot(self) -> dict:
        return {
            "id": self.id, "provider": self.provider, "device": self.device, "mode": self.mode, "name": self.name, "state": self.state,
            "camera": self.camera, "battery": self.battery, "radio": self.radio, "started_at": self.started_at,
            "progress": self.progress.snapshot() if self.progress else None,
        }


# Runs the offload; returns the device's display name and the engine result.
Flow = Callable[[Session], Awaitable[tuple[str, RunResult]]]
