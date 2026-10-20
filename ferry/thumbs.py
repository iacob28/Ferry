"""Library thumbnails for sources that don't serve their own (USB drives, printers)."""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
from fractions import Fraction
from pathlib import Path

# Enough of a JPEG to find the EXIF block (it sits in the first APP1 segment).
EXIF_SCAN = 256 << 10
FFMPEG_TIMEOUT = 30.0
WIDTH = 480


class ThumbnailError(Exception):
    pass


def exif_thumbnail(head: bytes) -> bytes:
    """The JPEG preview embedded in a photo's EXIF data (cameras write a 160x120 one)."""
    start = head.find(b"\xff\xd8", 2)  # the first SOI marker after the photo's own
    if start < 0:
        raise ThumbnailError("no embedded preview")
    end = head.find(b"\xff\xd9", start)
    if end < 0:
        raise ThumbnailError("embedded preview is cut off")
    return head[start:end + 2]


def _av_thumbnail(path: Path, at: float) -> bytes:
    import av  # optional: the "media" extra, included in the Docker image

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        if at and stream.duration and stream.time_base:
            if float(stream.duration * stream.time_base) > at:
                container.seek(int(at / stream.time_base), stream=stream)
        frame = next(container.decode(stream), None)
        if frame is None:
            raise ThumbnailError("no video frame")
        height = max(2, round(frame.height * WIDTH / frame.width / 2) * 2)
        frame = frame.reformat(width=WIDTH, height=height, format="yuvj420p")
        encoder = av.CodecContext.create("mjpeg", "w")
        encoder.width, encoder.height, encoder.pix_fmt = WIDTH, height, "yuvj420p"
        encoder.time_base = Fraction(1, 25)
        packets = [*encoder.encode(frame), *encoder.encode(None)]
        if not packets:
            raise ThumbnailError("could not encode the preview")
        return bytes(packets[0])


async def video_thumbnail(path: Path, at: float = 1.0) -> bytes:
    """One frame of a video (or a still image, with at=0) as a small JPEG, via PyAV or ffmpeg."""
    if importlib.util.find_spec("av") is not None:
        try:
            return await asyncio.wait_for(asyncio.to_thread(_av_thumbnail, path, at), FFMPEG_TIMEOUT)
        except ThumbnailError:
            raise
        except Exception as exc:
            raise ThumbnailError(f"cannot decode {path.name}: {exc}") from exc
    if shutil.which("ffmpeg") is None:
        raise ThumbnailError("needs PyAV (pip install 'ferry[media]') or the ffmpeg command")
    seek = ["-ss", str(at)] if at else []  # seeking breaks still images
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-loglevel", "error", *seek, "-i", str(path),
        "-frames:v", "1", "-vf", f"scale={WIDTH}:-2", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ThumbnailError("ffmpeg timed out") from None
    if proc.returncode != 0 or not out:
        if at > 0:  # Clips shorter than a second: take the first frame.
            return await video_thumbnail(path, 0)
        raise ThumbnailError(err.decode(errors="replace").strip()[-200:] or "ffmpeg produced no image")
    return out
