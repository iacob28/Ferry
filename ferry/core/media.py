"""What a source device offers, independent of how the device is reached."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class SourceError(Exception):
    """A device request failed; downloads that raise it are retried."""


@dataclass(frozen=True)
class MediaItem:
    # Unique on the device, e.g. "100GOPRO/GX010001.MP4"; part of the "already synced" key.
    path: str
    name: str
    # Device wall-clock time encoded as an epoch (cameras have no timezone).
    created: int
    # None when the device only knows the size on download (GoPro photo groups).
    size: int | None


class MediaSource(Protocol):
    async def list_media(self) -> list[MediaItem]: ...

    async def download(self, item: MediaItem, dest: Path, on_bytes: Callable[[int], None]) -> int:
        """Download into dest, resuming from its current size where the device allows it.

        Returns the expected total size. on_bytes receives only newly transferred bytes
        (negative if a resume is discarded).
        """

    async def delete(self, item: MediaItem) -> None: ...

    async def thumbnail(self, item: MediaItem) -> bytes: ...


def threadsafe(on_bytes: Callable[[int], None]) -> Callable[[int], None]:
    """Wraps a progress callback so a worker thread (to_thread copies) can call it.

    The engine's callback publishes events into asyncio queues, which only the loop may touch.
    """
    loop = asyncio.get_running_loop()
    return lambda n: loop.call_soon_threadsafe(on_bytes, n)
