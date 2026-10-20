"""Destinations for offloaded media: an SMB share (Synology) or a local folder."""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO, Callable

from .config import Settings, SmbSettings

CHUNK = 8 << 20


class TargetError(Exception):
    pass


class Target(ABC):
    @abstractmethod
    def describe(self) -> str: ...

    @abstractmethod
    def _size_of(self, rel: str) -> int | None: ...

    @abstractmethod
    def _upload(self, local: Path, rel: str, on_bytes: Callable[[int], None]) -> None: ...

    @abstractmethod
    def _test(self) -> None: ...

    @abstractmethod
    def open_read(self, rel: str) -> BinaryIO:
        """Blocking: a seekable binary file on the target (used to stream the library)."""

    async def size_of(self, rel: str) -> int | None:
        return await asyncio.to_thread(self._size_of, rel)

    async def upload(self, local: Path, rel: str, on_bytes: Callable[[int], None]) -> None:
        await asyncio.to_thread(self._upload, local, rel, on_bytes)

    async def test(self) -> None:
        await asyncio.to_thread(self._test)


def _copy(src, dst, on_bytes: Callable[[int], None]) -> None:
    while chunk := src.read(CHUNK):
        dst.write(chunk)
        on_bytes(len(chunk))


class LocalTarget(Target):
    def __init__(self, root: str):
        if not root:
            raise TargetError("local target path is not configured")
        self.root = Path(root)

    def describe(self) -> str:
        return str(self.root)

    def _size_of(self, rel: str) -> int | None:
        path = self.root / rel
        return path.stat().st_size if path.exists() else None

    def _upload(self, local: Path, rel: str, on_bytes: Callable[[int], None]) -> None:
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".partial")
        with open(local, "rb") as src, open(partial, "wb") as dst:
            _copy(src, dst, on_bytes)
        os.replace(partial, dest)

    def open_read(self, rel: str) -> BinaryIO:
        return open(self.root / rel, "rb")

    def _test(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        probe = self.root / ".gopro-sync-test"
        probe.write_text("ok")
        probe.unlink()


class SmbTarget(Target):
    def __init__(self, cfg: SmbSettings):
        if not (cfg.host and cfg.share):
            raise TargetError("SMB host and share must be configured")
        self.cfg = cfg
        parts = [p for p in cfg.base_path.replace("\\", "/").split("/") if p]
        self.root = "\\".join([rf"\\{cfg.host}\{cfg.share}", *parts])

    def describe(self) -> str:
        return self.root

    def _path(self, rel: str) -> str:
        return "\\".join([self.root, *[p for p in rel.split("/") if p]])

    def _session(self) -> None:
        import smbclient

        smbclient.register_session(
            self.cfg.host,
            username=self.cfg.username or None,
            password=self.cfg.password or None,
            connection_timeout=15,
        )

    def _size_of(self, rel: str) -> int | None:
        import smbclient

        self._session()
        try:
            return smbclient.stat(self._path(rel)).st_size
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return None
            raise

    def _upload(self, local: Path, rel: str, on_bytes: Callable[[int], None]) -> None:
        import smbclient

        self._session()
        dest = self._path(rel)
        partial = dest + ".partial"
        smbclient.makedirs(dest.rsplit("\\", 1)[0], exist_ok=True)
        with open(local, "rb") as src, smbclient.open_file(partial, mode="wb") as dst:
            _copy(src, dst, on_bytes)
        if self._size_of(rel) is not None:
            smbclient.remove(dest)
        smbclient.rename(partial, dest)

    def open_read(self, rel: str) -> BinaryIO:
        import smbclient

        self._session()
        # Shared read: browsers open several ranges of one video at once (GoPro puts the index at the end).
        return smbclient.open_file(self._path(rel), mode="rb", share_access="r")

    def _test(self) -> None:
        import smbclient

        self._session()
        smbclient.makedirs(self.root, exist_ok=True)
        probe = self.root + "\\.gopro-sync-test"
        with smbclient.open_file(probe, mode="w") as fh:
            fh.write("ok")
        smbclient.remove(probe)


def make_target(settings: Settings) -> Target:
    if settings.target == "local":
        return LocalTarget(settings.local_path)
    return SmbTarget(settings.smb)


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free
