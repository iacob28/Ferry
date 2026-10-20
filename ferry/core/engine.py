"""Offload engine: list device media, download new files, upload, verify, optionally delete."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from ..destinations import Destination, DestinationError
from ..thumbs import video_thumbnail
from .media import MediaItem, MediaSource, SourceError
from .settings import SyncPolicy, template_fields
from .store import Store

DOWNLOAD_ATTEMPTS = 4
STILLS = {"jpg", "jpeg", "png", "heic"}
DECODABLE = STILLS | {"mp4", "mov", "avi", "mts", "mkv"}
# Keep this much space free in the staging area on top of the file being downloaded.
STAGING_RESERVE = 2 << 30
# A file whose download fails in this many runs is left on the device and no longer tried.
GIVE_UP_AFTER_RUNS = 3
# This many files failing back to back means the link is down, not that the files are bad.
FAILURE_STREAK = 2


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
    failed: list[str] = field(default_factory=list)  # Files left on the device after their download failed.

    @property
    def problem(self) -> str | None:
        if not self.failed:
            return None
        names = ", ".join(self.failed[:3]) + (f" and {len(self.failed) - 3} more" if len(self.failed) > 3 else "")
        return f"{len(self.failed)} file(s) could not be copied and stay on the device: {names}"


@dataclass
class DeviceInfo:
    """Who is being offloaded: feeds the store keys and the path placeholders."""

    provider: str  # provider kind, e.g. "gopro"
    provider_label: str  # e.g. "GoPro"
    id: str  # stable device id (serial number, volume UUID, ...)
    model: str  # e.g. "HERO9 Black"
    name: str = ""  # the user's name for the device; falls back to the model


def dest_rel(policy: SyncPolicy, device: DeviceInfo, media: MediaItem) -> str:
    fields = template_fields(media.created, device.provider_label, device.name or device.model, device.model)
    return policy.key(fields, media.name)


def thumb_name(rel: str) -> str:
    return hashlib.sha1(rel.encode()).hexdigest() + ".jpg"


def _with_suffix(rel: str, created: int) -> str:
    base, dot, ext = rel.rpartition(".")
    return f"{base}_{created}.{ext}" if dot else f"{rel}_{created}"


class SyncEngine:
    def __init__(self, store: Store, policy: SyncPolicy, destination: Destination, staging: Path,
                 log: Callable[[str, str], None], on_progress: Callable[[Progress], None],
                 thumbs: Path | None = None, mirrors: list[Destination] | None = None):
        self.store = store
        # Every file must reach all of them before it counts as synced (and may be deleted).
        self.mirrors = list(mirrors or [])
        self.thumbs = thumbs
        self.policy = policy
        self.destination = destination
        self.staging = staging
        self.log = log
        self.on_progress = on_progress
        self.progress = Progress()

    async def run(self, source: MediaSource, device: DeviceInfo,
                  keep_alive: Callable[[], Awaitable[None]] | None = None) -> RunResult:
        try:
            return await self._run(source, device, keep_alive)
        finally:
            for destination in (self.destination, *self.mirrors):
                await asyncio.to_thread(destination.close)  # SFTP/FTP keep a connection open per run

    async def _run(self, source: MediaSource, device: DeviceInfo,
                   keep_alive: Callable[[], Awaitable[None]] | None) -> RunResult:
        device_id = device.id
        staging = self.staging / device_id.replace(":", "_")
        staging.mkdir(parents=True, exist_ok=True)

        media = await source.list_media()
        pending = [m for m in media if not self.store.is_synced(device_id, m.path, m.created, _key_size(m))]
        given_up = [m for m in pending
                    if self.store.failed_runs(device_id, m.path, m.created, _key_size(m)) >= GIVE_UP_AFTER_RUNS]
        if given_up:
            pending = [m for m in pending if m not in given_up]
            self.log("warning", f"not trying {len(given_up)} file(s) that failed in {GIVE_UP_AFTER_RUNS} runs: "
                                + ", ".join(m.name for m in given_up))
        self.progress.file_count = len(pending)
        self.progress.total_bytes = sum(m.size or 0 for m in pending)
        self.log("info", f"{len(media)} files on the device, {len(pending)} new "
                         f"({self.progress.total_bytes / 1e9:.1f} GB)")
        synced = [m for m in media if m not in pending and m not in given_up]
        await self._backfill_thumbnails(source, device_id, synced)
        # Synced in an earlier run (before deleting was switched on, or the link dropped while
        # deleting) and still on the device: deleted too, once the target still holds them.
        leftovers = [m for m in synced if not self.store.is_deleted(device_id, m.path, m.created, _key_size(m))]
        if not pending:
            result = RunResult(0, 0, 0, 0)
            if self.policy.delete_after_sync:
                await self._delete(source, device_id, [], leftovers, result)
            self.progress.phase = "done"
            self._emit()
            return result

        self.progress.phase = "transferring"
        queue: asyncio.Queue[tuple[MediaItem, Path, int] | None] = asyncio.Queue(maxsize=1)
        uploaded: list[tuple[MediaItem, int]] = []
        result = RunResult(0, 0, 0, 0)

        async def producer() -> None:
            streak = 0
            for m in pending:
                local = staging / f"{m.created}_{m.name}.part"
                try:
                    size = await self._download(source, m, local)
                except SourceError as exc:
                    streak += 1
                    if streak >= FAILURE_STREAK:
                        raise
                    # One bad file (corrupt on the card, refused by the device) must not block the rest.
                    local.unlink(missing_ok=True)
                    self.progress.file_count -= 1
                    self.progress.total_bytes -= m.size or 0
                    runs = self.store.note_failure(device_id, m.path, m.created, _key_size(m), str(exc))
                    self.log("warning", f"skipping {m.name} ({exc}); failed in {runs} of {GIVE_UP_AFTER_RUNS} runs")
                    result.failed.append(m.name)
                    continue
                streak = 0
                self.store.clear_failure(device_id, m.path, m.created, _key_size(m))
                await queue.put((m, local, size))
            await queue.put(None)

        async def consumer() -> None:
            while (item := await queue.get()) is not None:
                m, local, size = item
                existed, rel = await self._upload(m, local, size, device)
                await self._save_thumbnail(source, m, rel, local)
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

        if self.policy.delete_after_sync:
            # Only after every upload is verified, and while the device link is still up.
            await self._delete(source, device_id, [m for m, _ in uploaded], leftovers, result)

        self.progress.phase = "done"
        self._emit()
        return result

    async def _delete(self, source: MediaSource, device_id: str, uploaded: list[MediaItem],
                      leftovers: list[MediaItem], result: RunResult) -> None:
        verified = list(uploaded)
        for m in leftovers:
            rel = self.store.dest_of(device_id, m.path, m.created, _key_size(m))
            try:
                remote = await self.destination.stat(rel) if rel else None
            except DestinationError as exc:
                self.log("warning", f"cannot check {rel} on the target, keeping it on the device: {exc}")
                continue
            if remote is None or (m.size is not None and remote.size != m.size):
                self.log("warning", f"{m.path} is no longer on the target as {rel}, keeping it on the device")
                continue
            verified.append(m)
        if not verified:
            return
        self.progress.phase = "deleting"
        self._emit()
        for m in verified:
            try:
                await source.delete(m)
            except SourceError as exc:
                self.log("warning", f"could not delete {m.path} from the device: {exc}")
                continue
            self.store.mark_deleted(device_id, m.path, m.created, _key_size(m))
            result.deleted += 1
        self.log("info", f"deleted {result.deleted} files from the device")

    async def _download(self, source: MediaSource, m: MediaItem, local: Path) -> int:
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
                size = await source.download(m, local, self._count_download)
            except SourceError as exc:
                if attempt == DOWNLOAD_ATTEMPTS:
                    raise
                self.log("warning", f"{m.name}: {exc}; retrying ({attempt}/{DOWNLOAD_ATTEMPTS - 1})")
                await asyncio.sleep(2 * attempt)
                continue
            actual = local.stat().st_size
            if actual != size:
                local.unlink(missing_ok=True)
                raise SourceError(f"{m.name}: downloaded {actual} bytes, expected {size}")
            if m.size is None:
                self.progress.total_bytes += size
            return size
        raise SyncError(f"{m.name}: download failed")

    def _count_download(self, n: int) -> None:
        self.progress.downloaded_bytes += n
        self._emit(throttle=True)

    def _count_upload(self, n: int) -> None:
        self.progress.uploaded_bytes += n

    async def _backfill_thumbnails(self, source: MediaSource, device_id: str, synced: list[MediaItem]) -> None:
        """Fetches library thumbnails for files synced before thumbnails existed (or whose fetch failed)."""
        if self.thumbs is None:
            return
        for m in synced:
            rel = self.store.dest_of(device_id, m.path, m.created, _key_size(m))
            if rel and not (self.thumbs / thumb_name(rel)).exists():
                await self._save_thumbnail(source, m, rel)

    async def _save_thumbnail(self, source: MediaSource, m: MediaItem, rel: str, local: Path | None = None) -> None:
        if self.thumbs is None:
            return
        try:
            try:
                data = await source.thumbnail(m)
            except Exception:
                # The device has no preview: decode the staged copy instead, while it is still here.
                ext = m.name.rsplit(".", 1)[-1].lower()
                if local is None or ext not in DECODABLE:
                    raise
                data = await video_thumbnail(local, at=0 if ext in STILLS else 1.0)
            self.thumbs.mkdir(parents=True, exist_ok=True)
            (self.thumbs / thumb_name(rel)).write_bytes(data)
        except Exception as exc:  # Thumbnails are cosmetic: never fail a sync over one.
            self.log("warning", f"no thumbnail for {m.name}: {exc}")

    async def _upload(self, m: MediaItem, local: Path, size: int, device: DeviceInfo) -> tuple[bool, str]:
        rel = dest_rel(self.policy, device, m)
        self.progress.current_upload = rel
        self._emit()
        existing = await self.destination.stat(rel)
        if existing is not None and existing.size != size:
            # Same name, different file (cameras reuse numbers after a card format).
            rel = _with_suffix(rel, m.created)
            existing = await self.destination.stat(rel)
        already_there = existing is not None and existing.size == size
        if already_there:
            self.log("info", f"{rel} already on target, skipping upload")
            self.progress.uploaded_bytes += size
        else:
            await self._put_verified(self.destination, local, rel, size, self._count_upload)
        for mirror in self.mirrors:
            existing = await mirror.stat(rel)
            if existing is None or existing.size != size:
                await self._put_verified(mirror, local, rel, size, lambda n: None)
        self.store.mark_synced(device.id, m.path, m.created, _key_size(m), rel,
                               provider=device.provider, dest_id=self.policy.destination,
                               cred_id=self.policy.credentials)
        self.progress.current_upload = ""
        return already_there, rel

    async def _put_verified(self, destination: Destination, local: Path, rel: str, size: int,
                            on_bytes: Callable[[int], None]) -> None:
        remote = await destination.put(local, rel, on_bytes)
        if remote is None or remote.size != size:
            found = remote.size if remote else None
            raise DestinationError(f"{destination.describe(rel)}: has {found} bytes, expected {size}")

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


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _key_size(m: MediaItem) -> int:
    # Grouped photos have no size in the media list; key them consistently with -1.
    return m.size if m.size is not None else -1
