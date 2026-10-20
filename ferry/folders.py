"""Browse folders on the server for the folder picker (local destinations, watch folders).

Inside Docker these are the container's folders: host folders appear only where they are
mounted as volumes, so the mounted volumes are offered as shortcuts.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MAX_ENTRIES = 500
# Pseudo and system file systems that are never a place for media.
SYSTEM_FS = {"proc", "sysfs", "devpts", "devtmpfs", "tmpfs", "cgroup", "cgroup2", "mqueue", "overlay", "securityfs",
             "debugfs", "tracefs", "pstore", "bpf", "autofs", "hugetlbfs", "configfs", "fusectl", "binfmt_misc",
             "nsfs", "rpc_pipefs", "efivarfs", "ramfs", "squashfs"}
SYSTEM_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/etc", "/usr", "/boot")


class FolderError(Exception):
    pass


def _mounts() -> list[str]:
    try:
        lines = Path("/proc/self/mounts").read_text().splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        point = parts[1].replace("\\040", " ")
        if parts[2] in SYSTEM_FS or point == "/" or point.startswith(SYSTEM_PREFIXES):
            continue
        found.append(point)
    return sorted(set(found))


def shortcuts(data_dir: Path) -> list[dict]:
    """Starting points: mounted volumes first (where host folders show up), then the usual places."""
    places = [(p, "volume") for p in _mounts()]
    if sys.platform == "darwin":
        places.append(("/Volumes", "volume"))
    places += [(str(Path.home()), "home"), ("/", "root")]
    seen, out = set(), []
    for path, kind in places:
        if path not in seen and Path(path).is_dir() and not Path(path).resolve().is_relative_to(data_dir.resolve() / "run"):
            seen.add(path)
            out.append({"path": path, "kind": kind})
    return out


def list_dirs(path: str) -> dict:
    folder = Path(path or "/").expanduser()
    if not folder.is_absolute():
        raise FolderError("use an absolute path")
    try:
        folder = folder.resolve()
        entries = sorted((e for e in os.scandir(folder) if not e.name.startswith(".")), key=lambda e: e.name.lower())
    except FileNotFoundError:
        raise FolderError(f"{folder} does not exist") from None
    except NotADirectoryError:
        raise FolderError(f"{folder} is not a folder") from None
    except PermissionError:
        raise FolderError(f"no permission to read {folder}") from None
    dirs = []
    for entry in entries:
        try:
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": str(folder / entry.name)})
        except OSError:
            continue
        if len(dirs) >= MAX_ENTRIES:
            break
    return {"path": str(folder), "parent": str(folder.parent) if folder.parent != folder else None,
            "dirs": dirs, "writable": os.access(folder, os.W_OK), "truncated": len(dirs) >= MAX_ENTRIES}


def make_dir(parent: str, name: str) -> str:
    name = name.strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise FolderError("enter a folder name without slashes")
    target = Path(parent).resolve() / name
    try:
        target.mkdir(exist_ok=False)
    except FileExistsError:
        raise FolderError(f"{target} already exists") from None
    except OSError as exc:
        raise FolderError(f"cannot create {target}: {exc.strerror or exc}") from None
    return str(target)
