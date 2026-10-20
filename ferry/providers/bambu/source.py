"""The printer's SD card as a MediaSource."""

from __future__ import annotations

import asyncio
import posixpath
import time
from pathlib import Path
from typing import Callable

from ...core.media import MediaItem, SourceError, threadsafe
from ...core.names import time_from_name
from .ftps import FtpsError, PrinterFiles


class PrinterSource:
    def __init__(self, files: PrinterFiles, folders: list[str], extensions: list[str]):
        self.files = files
        self.folders = [f.strip("/") for f in folders if f.strip("/")]
        self.extensions = {e.lower().lstrip(".") for e in extensions}
        # One FTP connection: the engine downloads the next file while it uploads (and asks for
        # the thumbnail of) the previous one, so every command waits its turn.
        self._lock = asyncio.Lock()

    async def _call(self, fn, *args):
        async with self._lock:
            try:
                return await asyncio.to_thread(fn, *args)
            except FtpsError as exc:
                raise SourceError(str(exc)) from exc

    async def list_media(self) -> list[MediaItem]:
        items = []
        for folder in self.folders:
            for f in await self._call(self.files.listdir, f"/{folder}"):
                ext = f.name.rsplit(".", 1)[-1].lower()
                if f.is_dir or f.size <= 0 or ext not in self.extensions:
                    continue  # A timelapse still being written has size 0.
                created = time_from_name(f.name) or int(time.time())
                items.append(MediaItem(f"{folder}/{f.name}", f.name, created, f.size))
        return items

    async def download(self, item: MediaItem, dest: Path, on_bytes: Callable[[int], None]) -> int:
        offset = dest.stat().st_size if dest.exists() else 0
        if item.size is not None and offset >= item.size:
            if offset == item.size:
                return offset
            dest.unlink()
            on_bytes(-offset)
            offset = 0
        await self._call(self.files.download, f"/{item.path}", dest, offset, threadsafe(on_bytes))
        return item.size or dest.stat().st_size

    async def delete(self, item: MediaItem) -> None:
        await self._call(self.files.delete, f"/{item.path}")

    async def thumbnail(self, item: MediaItem) -> bytes:
        # Bambu printers keep a preview per timelapse in <folder>/thumbnail/<name>.jpg.
        folder, name = posixpath.split(item.path)
        stem = name.rsplit(".", 1)[0]
        return await self._call(self.files.read, f"/{folder}/thumbnail/{stem}.jpg")
