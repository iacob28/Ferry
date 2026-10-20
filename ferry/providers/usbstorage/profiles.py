"""What kind of device a DCIM volume came from, and how to read its file names."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    key: str
    label: str  # becomes {provider} in paths: "DJI/2026/..." rather than "USB storage/..."
    vendors: frozenset[str]
    folders: re.Pattern  # DCIM sub-folders this maker writes
    icon: str = "ph-camera"


PROFILES = [
    Profile("dji", "DJI", frozenset({"2ca3"}), re.compile(r"^(DJI_\d+|\d{3}MEDIA|PANORAMA)$", re.I), "ph-drone"),
    Profile("insta360", "Insta360", frozenset({"2e1a"}), re.compile(r"^Camera\d+$", re.I), "ph-camera-rotate"),
    Profile("gopro", "GoPro", frozenset({"2672"}), re.compile(r"^\d{3}GOPRO$", re.I), "ph-video-camera"),
]
GENERIC = Profile("camera", "Camera", frozenset(), re.compile(r".*"), "ph-camera")

# Media worth keeping; low-resolution proxies (LRF, LRV), thumbnails (THM) and telemetry stay behind.
DEFAULT_EXTENSIONS = ("mp4", "mov", "jpg", "jpeg", "dng", "heic", "insv", "insp", "avi", "mts", "arw", "cr2",
                      "cr3", "nef", "raf", "rw2", "orf", "wav")

# Capture time in the file name beats the file system's timestamps:
#   DJI_20261020143012_0001_D.MP4, DJI_0001.JPG has none.
#   VID_20261020_143012_00_001.insv (Insta360), PXL_20261020_143012123.mp4, 20261020_143012.jpg
_NAME_TIMES = [
    re.compile(r"(?<!\d)(?P<y>20\d\d)(?P<mo>[01]\d)(?P<d>[0-3]\d)(?P<h>[0-2]\d)(?P<mi>[0-5]\d)(?P<s>[0-5]\d)(?!\d)"),
    re.compile(r"(?<!\d)(?P<y>20\d\d)(?P<mo>[01]\d)(?P<d>[0-3]\d)[_-](?P<h>[0-2]\d)(?P<mi>[0-5]\d)(?P<s>[0-5]\d)"),
]


def identify(vendor: str, dcim_folders: list[str]) -> Profile:
    for profile in PROFILES:
        if vendor and vendor.lower() in profile.vendors:
            return profile
    for profile in PROFILES:
        if dcim_folders and all(profile.folders.match(f) for f in dcim_folders):
            return profile
    return GENERIC


def time_from_name(name: str) -> int | None:
    """Wall-clock capture time encoded as an epoch, like every other source."""
    for pattern in _NAME_TIMES:
        m = pattern.search(name)
        if m:
            try:
                return calendar.timegm((int(m["y"]), int(m["mo"]), int(m["d"]), int(m["h"]), int(m["mi"]), int(m["s"])))
            except ValueError:
                continue
    return None
