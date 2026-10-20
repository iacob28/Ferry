"""Capture times that devices encode in file names."""

from __future__ import annotations

import calendar
import re

#   DJI_20261020143012_0001_D.MP4 (DJI), VID_20261020_143012_00_001.insv (Insta360),
#   PXL_20261020_143012123.mp4, 20261020_143012.jpg, video_2026-10-20_14-30-12.mp4 (Bambu Lab)
_NAME_TIMES = [
    re.compile(r"(?<!\d)(?P<y>20\d\d)(?P<mo>[01]\d)(?P<d>[0-3]\d)(?P<h>[0-2]\d)(?P<mi>[0-5]\d)(?P<s>[0-5]\d)(?!\d)"),
    re.compile(r"(?<!\d)(?P<y>20\d\d)(?P<mo>[01]\d)(?P<d>[0-3]\d)[_-](?P<h>[0-2]\d)(?P<mi>[0-5]\d)(?P<s>[0-5]\d)"),
    re.compile(r"(?<!\d)(?P<y>20\d\d)-(?P<mo>[01]\d)-(?P<d>[0-3]\d)[_ T](?P<h>[0-2]\d)[-:](?P<mi>[0-5]\d)[-:](?P<s>[0-5]\d)"),
]


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
