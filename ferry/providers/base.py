"""Source providers: one per device family (GoPro, USB storage, Bambu printers, ...).

A provider finds its devices (`watch`) and starts sessions through the service; the session's
flow opens the device as a MediaSource and hands it to the shared SyncEngine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.service import SyncService
    from ..core.session import Session


class Provider(ABC):
    kind: str
    label: str

    def __init__(self, service: SyncService):
        self.service = service
        # Address of the device being paired, if any (the UI shows a pairing state).
        self.pairing: str | None = None

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
