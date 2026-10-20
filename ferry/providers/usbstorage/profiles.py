"""What kind of device a DCIM volume came from, and how to read its file names."""

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


PROFILES = [
    Profile("dji", "DJI", frozenset({"2ca3"}), re.compile(r"^(DJI_\d+|\d{3}MEDIA|PANORAMA)$", re.I), "ph-drone"),
    Profile("insta360", "Insta360", frozenset({"2e1a"}), re.compile(r"^Camera\d+$", re.I), "ph-camera-rotate"),
    Profile("gopro", "GoPro", frozenset({"2672"}), re.compile(r"^\d{3}GOPRO$", re.I), "ph-video-camera"),
]
GENERIC = Profile("camera", "Camera", frozenset(), re.compile(r".*"), "ph-camera")

# Media worth keeping; low-resolution proxies (LRF, LRV), thumbnails (THM) and telemetry stay behind.
DEFAULT_EXTENSIONS = ("mp4", "mov", "jpg", "jpeg", "dng", "heic", "insv", "insp", "avi", "mts", "arw", "cr2",
                      "cr3", "nef", "raf", "rw2", "orf", "wav")


def identify(vendor: str, dcim_folders: list[str]) -> Profile:
    for profile in PROFILES:
        if vendor and vendor.lower() in profile.vendors:
            return profile
    for profile in PROFILES:
        if dcim_folders and all(profile.folders.match(f) for f in dcim_folders):
            return profile
    return GENERIC
