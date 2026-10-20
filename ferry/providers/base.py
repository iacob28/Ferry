"""Source providers: one per device family (GoPro, USB storage, Bambu printers, ...).

A provider finds its devices (`watch`) and starts sessions through the service; the session's
flow opens the device as a MediaSource and hands it to the shared SyncEngine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..core.settings import Device, Settings

if TYPE_CHECKING:
    from ..core.service import SyncService
    from ..core.session import Session


class NoOptions(BaseModel):
    pass


class Provider(ABC):
    kind: str
    label: str
    # Phosphor icon name for the UI.
    icon: str = "ph-device-mobile"
    # Validates settings.providers[kind].options.
    options_model: type[BaseModel] = NoOptions

    def __init__(self, service: SyncService):
        self.service = service
        # Address of the device being paired, if any (the UI shows a pairing state).
        self.pairing: str | None = None

    def options(self, settings: Settings) -> BaseModel:
        return self.options_model.model_validate(settings.provider(self.kind).options)

    def auto_sync(self, settings: Settings) -> bool:
        """Whether this provider may start syncs on its own (manual syncs always work)."""
        return settings.provider(self.kind).enabled

    def describe(self) -> dict:
        return {"kind": self.kind, "label": self.label, "icon": self.icon,
                "options": self.options_model().model_dump()}

    async def new_device(self, name: str, config: dict) -> Device:
        """Validates a device the user adds by hand (a printer by IP, ...)."""
        raise ValueError(f"{self.label} devices are added automatically")

    @abstractmethod
    async def watch(self) -> None:
        """Runs for the lifetime of the service: detect devices and start sessions."""

    def request_sync(self, mode: str, device: str | None) -> int:
        """Queues a manual sync; returns how many devices it applies to."""
        return 0

    def snapshot(self) -> dict:
        """Provider state merged into the service status."""
        return {}

    def session_finished(self, session: Session, ok: bool) -> None:
        """Called after each of this provider's sessions, successful or not."""
