"""What kind of device a volume (internal storage, or an SD card in a reader) came from."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...core.names import time_from_name  # noqa: F401  (re-exported for the USB source)


@dataclass(frozen=True)
class Profile:
    key: str
    label: str  # becomes {provider} in paths: "DJI/2026/..." rather than "USB storage/..."
    vendors: frozenset[str]
    folders: re.Pattern  # DCIM sub-folders this maker writes
    icon: str = "ph-camera"
    # Whose sync rules apply. A GoPro or Bambu card syncs as if it came from the camera or printer
    # itself (same rules, same "already synced" keys), so taking the card out changes nothing.
    rules: str = "usbstorage"
    # Folder scanned for media, and the folder item paths are relative to (matching the device's own API).
    media_dir: str = "DCIM"
    paths_from: str = ""
    extensions: tuple[str, ...] | None = None


PROFILES = [
    Profile("dji", "DJI", frozenset({"2ca3"}), re.compile(r"^(DJI_\d+|\d{3}MEDIA|PANORAMA)$", re.I), "ph-drone"),
    Profile("insta360", "Insta360", frozenset({"2e1a"}), re.compile(r"^Camera\d+$", re.I), "ph-camera-rotate"),
    # The camera's media API names files "100GOPRO/GX010001.MP4", relative to DCIM.
    Profile("gopro", "GoPro", frozenset({"2672"}), re.compile(r"^\d{3}GOPRO$", re.I), "ph-video-camera",
            rules="gopro", paths_from="DCIM"),
]
GENERIC = Profile("camera", "Camera", frozenset(), re.compile(r".*"), "ph-camera")
# A Bambu Lab printer's microSD card: no DCIM, timelapses in /timelapse ("timelapse/<name>", as over FTPS).
BAMBU = Profile("bambu", "Bambu Lab", frozenset(), re.compile(r".*"), "ph-cube", rules="bambu",
                media_dir="timelapse", extensions=("mp4", "avi"))

# Media worth keeping; low-resolution proxies (LRF, LRV), thumbnails (THM) and telemetry stay behind.
DEFAULT_EXTENSIONS = ("mp4", "mov", "jpg", "jpeg", "dng", "heic", "insv", "insp", "avi", "mts", "arw", "cr2",
                      "cr3", "nef", "raf", "rw2", "orf", "wav")


def identify_volume(root, vendor: str) -> tuple[Profile, list[str]] | None:
    """The profile of a mounted volume and its media folders, or None if it holds no camera media."""
    dcim = root / "DCIM"
    if dcim.is_dir():
        folders = sorted(p.name for p in dcim.iterdir() if p.is_dir() and not p.name.startswith("."))
        return identify(vendor, folders), folders
    if (root / "timelapse").is_dir():
        return BAMBU, ["timelapse"]
    return None


def gopro_card_info(root) -> dict[str, str]:
    """Serial and model from MISC/version.txt, which GoPro cameras write to every card."""
    try:
        text = (root / "MISC" / "version.txt").read_text(errors="replace")
    except OSError:
        return {}
    info = {}
    for key, name in (("serial", "camera serial number"), ("model", "camera type")):
        m = re.search(rf'"{name}"\s*:\s*"([^"]+)"', text)
        if m:
            info[key] = m.group(1).strip()
    return info


def identify(vendor: str, dcim_folders: list[str]) -> Profile:
    for profile in PROFILES:
        if vendor and vendor.lower() in profile.vendors:
            return profile
    for profile in PROFILES:
        if dcim_folders and all(profile.folders.match(f) for f in dcim_folders):
            return profile
    return GENERIC
