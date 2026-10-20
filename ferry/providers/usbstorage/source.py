"""A mounted DCIM volume as a MediaSource."""

from __future__ import annotations

import asyncio
import calendar
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

from ...core.media import MediaItem, SourceError
from ...thumbs import EXIF_SCAN, ThumbnailError, exif_thumbnail, video_thumbnail
from .profiles import DEFAULT_EXTENSIONS, time_from_name

CHUNK = 4 << 20
MAX_DEPTH = 3
VIDEO = {"mp4", "mov", "avi", "mts", "insv"}


def wall_clock(mtime: float, fstype: str) -> int:
    """The camera's wall-clock time as an epoch (cameras have no timezone).

    FAT is mounted with tz=UTC, so its timestamps already are wall clock. exFAT and host-mounted
    folders give real UTC; shift that into the server's timezone (set TZ like the camera's).
    """
    if fstype == "vfat":
        return int(mtime)
    return calendar.timegm(time.localtime(mtime))


class FolderSource:
    def __init__(self, root: Path, fstype: str = "", extensions: tuple[str, ...] = DEFAULT_EXTENSIONS,
                 make_writable: Callable[[], Awaitable[None]] | None = None):
        self.root = root
        self.fstype = fstype
        self.extensions = {e.lower().lstrip(".") for e in extensions}
        self._make_writable = make_writable
        self._writable = False

    def _scan(self) -> list[MediaItem]:
        dcim = self.root / "DCIM"
        items = []
        for dirpath, dirnames, filenames in os.walk(dcim):
            depth = len(Path(dirpath).relative_to(dcim).parts)
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")) if depth < MAX_DEPTH else []
            for name in sorted(filenames):
                if name.startswith(".") or name.rsplit(".", 1)[-1].lower() not in self.extensions:
                    continue
                path = Path(dirpath) / name
                st = path.stat()
                created = time_from_name(name) or wall_clock(st.st_mtime, self.fstype)
                items.append(MediaItem(str(path.relative_to(self.root)), name, created, st.st_size))
        return items

    async def list_media(self) -> list[MediaItem]:
        if not (self.root / "DCIM").is_dir():
            raise SourceError(f"{self.root} has no DCIM folder")
        return await asyncio.to_thread(self._scan)

    def _copy(self, src: Path, dest: Path, on_bytes: Callable[[int], None]) -> int:
        size = src.stat().st_size
        offset = dest.stat().st_size if dest.exists() else 0
        if offset > size:
            dest.unlink()
            offset = 0
        with open(src, "rb") as fin, open(dest, "ab" if offset else "wb") as fout:
            fin.seek(offset)
            while chunk := fin.read(CHUNK):
                fout.write(chunk)
                on_bytes(len(chunk))
        return size

    async def download(self, item: MediaItem, dest: Path, on_bytes: Callable[[int], None]) -> int:
        try:
            return await asyncio.to_thread(self._copy, self.root / item.path, dest, on_bytes)
        except OSError as exc:  # A drive unplugged mid-copy.
            raise SourceError(f"reading {item.path} failed: {exc}") from exc

    async def delete(self, item: MediaItem) -> None:
        if not self._writable and self._make_writable is not None:
            try:
                await self._make_writable()
            except Exception as exc:
                raise SourceError(f"cannot make the volume writable: {exc}") from exc
            self._writable = True
        try:
            (self.root / item.path).unlink()
        except OSError as exc:
            raise SourceError(str(exc)) from exc

    async def thumbnail(self, item: MediaItem) -> bytes:
        path = self.root / item.path
        ext = item.name.rsplit(".", 1)[-1].lower()
        if ext in VIDEO:
            # Some cameras write a small preview next to each clip.
            for sidecar in (".THM", ".thm"):
                side = path.with_suffix(sidecar)
                if side.exists():
                    return await asyncio.to_thread(side.read_bytes)
        try:
            if ext in ("jpg", "jpeg"):
                with open(path, "rb") as fh:
                    head = await asyncio.to_thread(fh.read, EXIF_SCAN)
                try:
                    return exif_thumbnail(head)
                except ThumbnailError:
                    return await video_thumbnail(path, at=0)  # ffmpeg scales still images too
            if ext in VIDEO:
                return await video_thumbnail(path)
        except (OSError, ThumbnailError) as exc:
            raise SourceError(f"no thumbnail: {exc}") from exc
        raise SourceError("no thumbnail for this file type")
