"""Where offloaded media goes. Keys are relative POSIX paths ("GoPro/2026/2026-10-20/GX010001.MP4");
each destination maps them onto its own storage (a share path, an object key, a folder chain)."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Callable

CHUNK = 8 << 20
READ_CHUNK = 1 << 20


class DestinationError(Exception):
    pass


@dataclass(frozen=True)
class RemoteObject:
    size: int
    # Hex digests, when the destination reports them (object stores, Drive).
    md5: str | None = None
    sha256: str | None = None


class Destination(ABC):
    kind: str

    @abstractmethod
    def describe(self, key: str = "") -> str: ...

    @abstractmethod
    def _stat(self, key: str) -> RemoteObject | None: ...

    @abstractmethod
    def _put(self, local: Path, key: str, on_bytes: Callable[[int], None]) -> None:
        """Blocking upload. Must be atomic: the key never shows a partial file."""

    @abstractmethod
    def _open_read(self, key: str) -> BinaryIO:
        """Blocking: a seekable binary file (the default open_range reads through it)."""

    @abstractmethod
    def _test(self) -> None: ...

    async def stat(self, key: str) -> RemoteObject | None:
        return await asyncio.to_thread(self._stat, key)

    async def put(self, local: Path, key: str, on_bytes: Callable[[int], None]) -> RemoteObject | None:
        """Uploads and returns what the destination now holds under key."""
        await asyncio.to_thread(self._put, local, key, on_bytes)
        return await self.stat(key)

    async def open_range(self, key: str, start: int, end: int) -> AsyncIterator[bytes]:
        """Yields bytes start..end (inclusive). Object stores override this with ranged GETs."""
        fh = await asyncio.to_thread(self._open_read, key)
        try:
            await asyncio.to_thread(fh.seek, start)
            left = end - start + 1
            while left > 0:
                chunk = await asyncio.to_thread(fh.read, min(READ_CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(fh.close)

    async def test(self) -> None:
        await asyncio.to_thread(self._test)


def copy_stream(src, dst, on_bytes: Callable[[int], None]) -> None:
    while chunk := src.read(CHUNK):
        dst.write(chunk)
        on_bytes(len(chunk))
