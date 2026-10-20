"""HTTP client for the Open GoPro API (same API over the camera's Wi-Fi AP and over USB)."""

from __future__ import annotations

import asyncio
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

WIFI_AP_HOST = "10.5.5.9"
HTTP_PORT = 8080


class CameraError(Exception):
    pass


@dataclass(frozen=True)
class MediaFile:
    directory: str
    name: str
    # Camera wall-clock time encoded as an epoch (the camera has no timezone).
    created: int
    # None for files inside a photo group, whose size is only known on download.
    size: int | None

    @property
    def path(self) -> str:
        return f"{self.directory}/{self.name}"


def parse_media_list(data: dict) -> list[MediaFile]:
    files: list[MediaFile] = []
    for folder in data.get("media", []):
        directory = folder["d"]
        for item in folder.get("fs", []):
            name = item["n"]
            created = int(item.get("cre", 0))
            if "b" in item and "l" in item:
                # Burst/time-lapse photo group: one entry covers G<grp><idx>.JPG for idx in [b, l].
                missing = {int(m) for m in item.get("m", [])}
                prefix, ext = name[:4], name.rsplit(".", 1)[-1]
                for idx in range(int(item["b"]), int(item["l"]) + 1):
                    if idx not in missing:
                        files.append(MediaFile(directory, f"{prefix}{idx:04d}.{ext}", created, None))
            else:
                size = int(item["s"]) if "s" in item else None
                files.append(MediaFile(directory, name, created, size))
    return files


def _with_path(url: str, path: str) -> str:
    # The camera rejects an encoded slash (100GOPRO%2FGX010001.MP4) with HTTP 400.
    return f"{url}?path={quote(path, safe='/')}"


class GoProHttp:
    def __init__(self, host: str, port: int = HTTP_PORT, interface: str | None = None):
        self.host = host
        transport = None
        if interface and sys.platform == "linux":
            # Every camera AP serves 10.5.5.9; pin traffic to the radio joined to this camera.
            transport = httpx.AsyncHTTPTransport(
                socket_options=[(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode())]
            )
        self._client = httpx.AsyncClient(
            base_url=f"http://{host}:{port}",
            timeout=httpx.Timeout(15.0, read=120.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, /, **params) -> httpx.Response:
        try:
            resp = await self._client.get(url, params=params or None)
        except httpx.HTTPError as exc:
            raise CameraError(f"GET {url} failed: {exc!r}") from exc
        return resp

    async def _get_json(self, url: str, /, **params) -> dict:
        resp = await self._get(url, **params)
        if resp.status_code != 200:
            raise CameraError(f"GET {url} returned HTTP {resp.status_code}")
        return resp.json() if resp.content.strip() else {}

    async def wait_ready(self, timeout: float = 30.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                await self._get_json("/gopro/version")
                return
            except CameraError:
                if asyncio.get_running_loop().time() > deadline:
                    raise CameraError(f"camera at {self.host} did not answer within {timeout:.0f}s")
                await asyncio.sleep(1)

    async def info(self) -> dict:
        try:
            return (await self._get_json("/gp/gpControl/info")).get("info", {})
        except CameraError:
            return {}

    async def media_list(self) -> list[MediaFile]:
        return parse_media_list(await self._get_json("/gopro/media/list"))

    async def battery_percent(self) -> int | None:
        try:
            state = await self._get_json("/gopro/camera/state")
        except CameraError:
            return None
        value = state.get("status", {}).get("70")
        return int(value) if value is not None else None

    async def keep_alive(self) -> None:
        try:
            await self._get("/gopro/camera/keep_alive")
        except CameraError:
            pass

    async def set_wired_control(self, enabled: bool) -> None:
        await self._get("/gopro/camera/control/wired_usb", p=1 if enabled else 0)

    async def delete(self, path: str) -> None:
        resp = await self._get(_with_path("/gopro/media/delete/file", path))
        if resp.status_code == 404:
            # Older firmware only has the legacy endpoint.
            resp = await self._get(f"/gp/gpControl/command/storage/delete?p={quote(path, safe='/')}")
        if resp.status_code != 200:
            raise CameraError(f"deleting {path} returned HTTP {resp.status_code}")

    async def thumbnail(self, path: str) -> bytes:
        resp = await self._get(_with_path("/gopro/media/thumbnail", path))
        if resp.status_code != 200 or not resp.content:
            raise CameraError(f"thumbnail {path}: HTTP {resp.status_code}")
        return resp.content

    async def sleep(self) -> None:
        try:
            await self._get("/gp/gpControl/command/system/sleep")
        except CameraError:
            pass

    async def download(self, media: MediaFile, dest: Path, on_bytes: Callable[[int], None]) -> int:
        """Download into dest, resuming from its current size. Returns the expected total size.

        on_bytes receives only newly transferred bytes (negative if a resume is discarded).
        """
        offset = dest.stat().st_size if dest.exists() else 0
        if media.size is not None and offset > media.size:
            dest.unlink()
            offset = 0
        if media.size is not None and offset == media.size:
            return offset
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            async with self._client.stream("GET", f"/videos/DCIM/{media.path}", headers=headers) as resp:
                if resp.status_code == 200 and offset:
                    # Range ignored: start over, and take the staged bytes back out of the progress.
                    on_bytes(-offset)
                    offset = 0
                elif resp.status_code not in (200, 206):
                    raise CameraError(f"download of {media.path} returned HTTP {resp.status_code}")
                expected = offset + int(resp.headers.get("content-length", 0))
                if media.size is not None:
                    expected = media.size
                with open(dest, "ab" if offset else "wb") as fh:
                    async for chunk in resp.aiter_bytes(1 << 20):
                        fh.write(chunk)
                        on_bytes(len(chunk))
        except httpx.HTTPError as exc:
            raise CameraError(f"download of {media.path} failed: {exc!r}") from exc
        return expected
