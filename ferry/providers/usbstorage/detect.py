"""Find USB mass-storage volumes (drones, cameras, card readers) and mount them read-only."""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ... import netutil

SYS_BLOCK = Path("/sys/block")
# Camera media is always FAT or exFAT (NTFS for the odd card reader); never touch anything else,
# so the host's own disks (ext4, btrfs, swap) are skipped without being mounted.
MEDIA_FILESYSTEMS = {"vfat", "exfat", "ntfs", "ntfs3"}
MOUNT_OPTIONS = {
    # tz=UTC: FAT stores wall-clock times; read them back unshifted (see source.wall_clock).
    "vfat": "ro,nosuid,nodev,noexec,utf8,tz=UTC",
    "exfat": "ro,nosuid,nodev,noexec,iocharset=utf8",
}
DEFAULT_OPTIONS = "ro,nosuid,nodev,noexec"


@dataclass(frozen=True)
class Candidate:
    """A volume that might hold camera media: a USB partition, or a folder under a watch root."""

    id: str  # "usb:<filesystem uuid>" or "dir:<hash>"
    source: str  # /dev/sda1, or the folder
    kind: str  # "block" | "dir"
    label: str = ""
    fstype: str = ""
    vendor: str = ""  # USB vendor id, e.g. "2ca3" (DJI)
    product: str = ""
    serial: str = ""


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _usb_parent(dev: Path) -> Path | None:
    """The USB device directory (with idVendor) above a block device, or None if not on USB."""
    node = dev.resolve()
    if "/usb" not in str(node):
        return None
    for _ in range(12):
        if (node / "idVendor").exists():
            return node
        if node.parent == node:
            break
        node = node.parent
    return None


async def _blkid(device: str) -> dict[str, str]:
    out = await netutil.run("blkid", "-o", "export", device, check=False, timeout=10)
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


async def block_candidates(sd_slot: bool = False) -> list[Candidate]:
    """USB disks and card readers; with sd_slot, also the host's built-in SD slot (mmcblk).

    The built-in slot is opt-in: on a Raspberry Pi that boots from its SD card it holds the
    system. Even then only FAT/exFAT/NTFS volumes with camera media are ever used.
    """
    if sys.platform != "linux" or not SYS_BLOCK.exists():
        return []
    found: list[Candidate] = []
    disks = sorted(SYS_BLOCK.glob("sd*")) + (sorted(SYS_BLOCK.glob("mmcblk[0-9]")) if sd_slot else [])
    for disk in disks:
        builtin = disk.name.startswith("mmcblk")
        usb = None if builtin else _usb_parent(disk / "device")
        if (usb is None and not builtin) or _read(disk / "size") in ("", "0"):
            continue  # Not USB, or a card reader without a card.
        partitions = sorted(p for p in disk.iterdir() if p.name.startswith(disk.name) and (p / "partition").exists())
        for part in partitions or [disk]:
            device = f"/dev/{part.name}"
            info = await _blkid(device)
            fstype = info.get("TYPE", "")
            if fstype not in MEDIA_FILESYSTEMS:
                continue
            serial = _read(usb / "serial") if usb else _read(disk / "device" / "serial")
            uuid = info.get("UUID") or f"{serial}-{part.name}"
            found.append(Candidate(
                id=f"usb:{uuid}", source=device, kind="block", label=info.get("LABEL", ""), fstype=fstype,
                vendor=_read(usb / "idVendor") if usb else "", product=_read(usb / "product") if usb else "SD card",
                serial=serial,
            ))
    return found


def dir_candidates(roots: list[str]) -> list[Candidate]:
    """Already-mounted volumes: every folder with camera media (DCIM, or a printer's timelapse) under a root."""
    found = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for child in [base, *sorted(p for p in base.iterdir() if p.is_dir())]:
            if (child / "DCIM").is_dir() or (child / "timelapse").is_dir():
                digest = hashlib.sha1(str(child.resolve()).encode()).hexdigest()[:12]
                found.append(Candidate(id=f"dir:{digest}", source=str(child), kind="dir", label=child.name))
    return found


async def mount(candidate: Candidate, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if os.path.ismount(target):
        return
    options = MOUNT_OPTIONS.get(candidate.fstype, DEFAULT_OPTIONS)
    await netutil.run("mount", "-t", candidate.fstype, "-o", options, candidate.source, str(target), timeout=30)


async def remount(target: Path, writable: bool) -> None:
    await netutil.run("mount", "-o", f"remount,{'rw' if writable else 'ro'}", str(target), timeout=30)


async def unmount(target: Path, lazy: bool = False) -> None:
    if os.path.ismount(target):
        await netutil.run("umount", *(["-l"] if lazy else []), str(target), check=False, timeout=30)
    try:
        target.rmdir()
    except OSError:
        pass
