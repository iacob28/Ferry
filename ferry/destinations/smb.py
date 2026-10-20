"""An SMB share (Synology, QNAP, TrueNAS, Windows, Samba)."""

from __future__ import annotations

import errno
from pathlib import Path
from typing import BinaryIO, Callable

from ..core.settings import Credential, DestinationConfig
from .base import Destination, DestinationError, RemoteObject, copy_stream


class SmbDestination(Destination):
    kind = "smb"
    credential_kinds = frozenset({"password"})

    def __init__(self, cfg: DestinationConfig, credential: Credential | None = None):
        if not (cfg.host and cfg.share):
            raise DestinationError("SMB host and share must be configured")
        self.cfg = cfg
        self.username = credential.username if credential else ""
        self.password = credential.password if credential else ""
        parts = [p for p in cfg.base_path.replace("\\", "/").split("/") if p]
        self.root = "\\".join([rf"\\{cfg.host}\{cfg.share}", *parts])

    def describe(self, key: str = "") -> str:
        return self._path(key) if key else self.root

    def _path(self, key: str) -> str:
        return "\\".join([self.root, *[p for p in key.split("/") if p]])

    def _session(self) -> None:
        import smbclient

        smbclient.register_session(
            self.cfg.host,
            username=self.username or None,
            password=self.password or None,
            connection_timeout=15,
        )

    def _stat(self, key: str) -> RemoteObject | None:
        import smbclient

        self._session()
        try:
            return RemoteObject(smbclient.stat(self._path(key)).st_size)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return None
            raise

    def _put(self, local: Path, key: str, on_bytes: Callable[[int], None]) -> None:
        import smbclient

        self._session()
        dest = self._path(key)
        partial = dest + ".partial"
        smbclient.makedirs(dest.rsplit("\\", 1)[0], exist_ok=True)
        with open(local, "rb") as src, smbclient.open_file(partial, mode="wb") as dst:
            copy_stream(src, dst, on_bytes)
        if self._stat(key) is not None:
            smbclient.remove(dest)
        smbclient.rename(partial, dest)

    def _open_read(self, key: str) -> BinaryIO:
        import smbclient

        self._session()
        # Shared read: browsers open several ranges of one video at once (GoPro puts the index at the end).
        return smbclient.open_file(self._path(key), mode="rb", share_access="r")

    def _test(self) -> None:
        import smbclient

        self._session()
        smbclient.makedirs(self.root, exist_ok=True)
        probe = self.root + "\\.ferry-test"
        with smbclient.open_file(probe, mode="w") as fh:
            fh.write("ok")
        smbclient.remove(probe)
