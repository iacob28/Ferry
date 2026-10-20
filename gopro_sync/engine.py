"""Offload engine: list camera media, download new files, upload, verify, optionally delete."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from .camera import CameraError, GoProHttp, MediaFile
from .config import Settings
from .store import Store
from .targets import Target, TargetError, free_bytes

DOWNLOAD_ATTEMPTS = 4
# Keep this much space free in the staging area on top of the file being downloaded.
STAGING_RESERVE = 2 << 30


class SyncError(Exception):
    pass


@dataclass
class Progress:
    phase: str = "listing"
    file_count: int = 0
    files_done: int = 0
    total_bytes: int = 0
    downloaded_bytes: int = 0
    uploaded_bytes: int = 0
    current_download: str = ""
    current_upload: str = ""
    started_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict:
        data = asdict(self)
        elapsed = max(time.time() - self.started_at, 1e-3)
        data["download_rate"] = self.downloaded_bytes / elapsed
        return data


@dataclass
class RunResult:
    files: int
    bytes: int
    deleted: int
    skipped_existing: int


def dest_rel(settings: Settings, camera_label: str, media: MediaFile) -> str:
    # The camera clock has no timezone, so read the epoch back as naive wall time.
    taken = datetime.fromtimestamp(media.created, tz=timezone.utc)
    folder = settings.folder_template.format(
        year=f"{taken:%Y}", month=f"{taken:%m}", day=f"{taken:%d}", date=f"{taken:%Y-%m-%d}", camera=camera_label,
    ).strip("/")
    return f"{folder}/{media.name}" if folder else media.name


def thumb_name(rel: str) -> str:
    return hashlib.sha1(rel.encode()).hexdigest() + ".jpg"


def _with_suffix(rel: str, created: int) -> str:
    base, dot, ext = rel.rpartition(".")
    return f"{base}_{created}.{ext}" if dot else f"{rel}_{created}"


class SyncEngine:
    def __init__(self, store: Store, settings: Settings, target: Target, staging: Path,
                 log: Callable[[str, str], None], on_progress: Callable[[Progress], None],
                 thumbs: Path | None = None):
        self.store = store
        self.thumbs = thumbs
        self.settings = settings
        self.target = target
        self.staging = staging
        self.log = log
        self.on_progress = on_progress
        self.progress = Progress()

    async def run(self, camera: GoProHttp, camera_id: str, camera_label: str,
                  keep_alive: Callable[[], Awaitable[None]] | None = None) -> RunResult:
        staging = self.staging / camera_id
        staging.mkdir(parents=True, exist_ok=True)

        media = await camera.media_list()
        pending = [m for m in media if not self.store.is_synced(camera_id, m.path, m.created, _key_size(m))]
        self.progress.file_count = len(pending)
        self.progress.total_bytes = sum(m.size or 0 for m in pending)
        self.log("info", f"{len(media)} files on camera, {len(pending)} new "
                         f"({self.progress.total_bytes / 1e9:.1f} GB)")
        await self._backfill_thumbnails(camera, camera_id, [m for m in media if m not in pending])
        if not pending:
            self.progress.phase = "done"
            self._emit()
            return RunResult(0, 0, 0, 0)

        self.progress.phase = "transferring"
        queue: asyncio.Queue[tuple[MediaFile, Path, int] | None] = asyncio.Queue(maxsize=1)
        uploaded: list[tuple[MediaFile, int]] = []
        result = RunResult(0, 0, 0, 0)

        async def producer() -> None:
            for m in pending:
                local = staging / f"{m.created}_{m.name}.part"
                size = await self._download(camera, m, local)
                await queue.put((m, local, size))
            await queue.put(None)

        async def consumer() -> None:
            while (item := await queue.get()) is not None:
                m, local, size = item
                existed, rel = await self._upload(m, local, size, camera_id, camera_label)
                await self._save_thumbnail(camera, m, rel)
                local.unlink(missing_ok=True)
                uploaded.append((m, size))
                result.files += 1
                result.bytes += size
                result.skipped_existing += existed
                self.progress.files_done += 1
                self._emit()

        tasks = [asyncio.create_task(producer()), asyncio.create_task(consumer())]
        if keep_alive is not None:
            tasks.append(asyncio.create_task(self._keep_alive_loop(keep_alive)))
        try:
            done, _ = await asyncio.wait(tasks[:2], return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
            await asyncio.gather(*tasks[:2])
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if self.settings.delete_after_sync:
            # Only after every upload is verified, and while the camera link is still up.
            self.progress.phase = "deleting"
            self._emit()
            for m, size in uploaded:
                try:
                    await camera.delete(m.path)
                except CameraError as exc:
                    self.log("warning", f"could not delete {m.path} from camera: {exc}")
                    continue
                self.store.mark_deleted(camera_id, m.path, m.created, _key_size(m))
                result.deleted += 1
            self.log("info", f"deleted {result.deleted} files from the camera")

        self.progress.phase = "done"
        self._emit()
        return result

    async def _download(self, camera: GoProHttp, m: MediaFile, local: Path) -> int:
        needed = (m.size or 0) - (local.stat().st_size if local.exists() else 0)
        if free_bytes(self.staging) < needed + STAGING_RESERVE:
            raise SyncError(f"not enough staging space for {m.name}")
        self.progress.current_download = m.path
        self._emit()
        base = self.progress.downloaded_bytes
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            # Count whatever is already staged (a resumed download) towards progress.
            self.progress.downloaded_bytes = base + (local.stat().st_size if local.exists() else 0)
            try:
                size = await camera.download(m, local, self._count_download)
            except CameraError as exc:
                if attempt == DOWNLOAD_ATTEMPTS:
                    raise
                self.log("warning", f"{m.name}: {exc}; retrying ({attempt}/{DOWNLOAD_ATTEMPTS - 1})")
                await asyncio.sleep(2 * attempt)
                continue
            actual = local.stat().st_size
            if actual != size:
                local.unlink(missing_ok=True)
                raise SyncError(f"{m.name}: downloaded {actual} bytes, expected {size}")
            if m.size is None:
                self.progress.total_bytes += size
            return size
        raise SyncError(f"{m.name}: download failed")

    def _count_download(self, n: int) -> None:
        self.progress.downloaded_bytes += n
        self._emit(throttle=True)

    def _count_upload(self, n: int) -> None:
        self.progress.uploaded_bytes += n

    async def _backfill_thumbnails(self, camera: GoProHttp, camera_id: str, synced: list[MediaFile]) -> None:
        """Fetches library thumbnails for files synced before thumbnails existed (or whose fetch failed)."""
        if self.thumbs is None:
            return
        for m in synced:
            rel = self.store.dest_of(camera_id, m.path, m.created, _key_size(m))
            if rel and not (self.thumbs / thumb_name(rel)).exists():
                await self._save_thumbnail(camera, m, rel)

    async def _save_thumbnail(self, camera: GoProHttp, m: MediaFile, rel: str) -> None:
        if self.thumbs is None:
            return
        try:
            data = await camera.thumbnail(m.path)
            self.thumbs.mkdir(parents=True, exist_ok=True)
            (self.thumbs / thumb_name(rel)).write_bytes(data)
        except Exception as exc:  # Thumbnails are cosmetic: never fail a sync over one.
            self.log("warning", f"no thumbnail for {m.name}: {exc}")

    async def _upload(self, m: MediaFile, local: Path, size: int, camera_id: str, label: str) -> tuple[bool, str]:
        rel = dest_rel(self.settings, label, m)
        self.progress.current_upload = rel
        self._emit()
        existing = await self.target.size_of(rel)
        if existing is not None and existing != size:
            # Same name, different file (the camera reuses numbers after a card format).
            rel = _with_suffix(rel, m.created)
            existing = await self.target.size_of(rel)
        already_there = existing == size
        if already_there:
            self.log("info", f"{rel} already on target, skipping upload")
            self.progress.uploaded_bytes += size
        else:
            await self.target.upload(local, rel, self._count_upload)
            remote = await self.target.size_of(rel)
            if remote != size:
                raise TargetError(f"{rel}: target has {remote} bytes, expected {size}")
        self.store.mark_synced(camera_id, m.path, m.created, _key_size(m), rel)
        self.progress.current_upload = ""
        return already_there, rel

    async def _keep_alive_loop(self, keep_alive: Callable[[], Awaitable[None]]) -> None:
        while True:
            try:
                await keep_alive()
            except Exception as exc:  # A missed keep-alive is not fatal.
                self.log("warning", f"keep-alive failed: {exc}")
            await asyncio.sleep(3)

    _last_emit = 0.0

    def _emit(self, throttle: bool = False) -> None:
        now = time.monotonic()
        if throttle and now - self._last_emit < 0.5:
            return
        self._last_emit = now
        self.on_progress(self.progress)


def _key_size(m: MediaFile) -> int:
    # Grouped photos have no size in the media list; key them consistently with -1.
    return m.size if m.size is not None else -1
