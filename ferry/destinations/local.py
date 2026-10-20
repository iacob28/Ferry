"""A folder on the host (or a share mounted into the container)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO, Callable

from .base import Destination, DestinationError, RemoteObject, copy_stream


class LocalDestination(Destination):
    kind = "local"

    def __init__(self, root: str):
        if not root:
            raise DestinationError("local target path is not configured")
        self.root = Path(root)

    def describe(self, key: str = "") -> str:
        return str(self.root / key) if key else str(self.root)

    def _stat(self, key: str) -> RemoteObject | None:
        path = self.root / key
        return RemoteObject(path.stat().st_size) if path.exists() else None

    def _put(self, local: Path, key: str, on_bytes: Callable[[int], None]) -> None:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".partial")
        with open(local, "rb") as src, open(partial, "wb") as dst:
            copy_stream(src, dst, on_bytes)
        os.replace(partial, dest)

    def _open_read(self, key: str) -> BinaryIO:
        return open(self.root / key, "rb")

    def _test(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        probe = self.root / ".ferry-test"
        probe.write_text("ok")
        probe.unlink()
