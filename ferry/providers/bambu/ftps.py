"""The printer's SD card over FTPS: implicit TLS on port 990, user bblp, the LAN access code."""

from __future__ import annotations

import ftplib
import re
from dataclasses import dataclass
from typing import Callable

from ...ftpclient import CertificateChanged, ImplicitFtps

PORT = 990
USER = "bblp"
TIMEOUT = 30
# "-rw-rw-rw-   1 root  root   1234567 Oct 20 14:30 video_2026-10-20_14-30-12.mp4"
_LIST_LINE = re.compile(r"^(?P<mode>[-dl])\S*\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+\w{3}\s+\d+\s+[\d:]+\s+(?P<name>.+)$")


class FtpsError(Exception):
    pass


@dataclass(frozen=True)
class RemoteFile:
    name: str
    size: int
    is_dir: bool = False


def parse_list(lines: list[str]) -> list[RemoteFile]:
    files = []
    for line in lines:
        m = _LIST_LINE.match(line.strip())
        if m and m["name"] not in (".", ".."):
            files.append(RemoteFile(m["name"], int(m["size"]), m["mode"] == "d"))
    return files


class PrinterFiles:
    """Blocking client; the provider runs it in a worker thread."""

    def __init__(self, host: str, access_code: str, fingerprint: str | None = None, port: int = PORT):
        self.host, self.port = host, port
        self.access_code = access_code
        self.ftp = ImplicitFtps(fingerprint, timeout=TIMEOUT)

    def open(self) -> PrinterFiles:
        try:
            self.ftp.connect(self.host, self.port)
            self.ftp.login(USER, self.access_code)
            self.ftp.prot_p()
        except CertificateChanged as exc:
            raise FtpsError(str(exc).replace("the server", "the printer")) from exc
        except ftplib.error_perm as exc:
            raise FtpsError(f"login refused ({exc}); check the access code on the printer screen") from exc
        except (OSError, EOFError, ftplib.Error) as exc:
            if isinstance(exc, FtpsError):
                raise
            raise FtpsError(f"cannot reach the printer at {self.host}:{self.port} ({exc})") from exc
        return self

    @property
    def fingerprint(self) -> str | None:
        return self.ftp.fingerprint

    def close(self) -> None:
        try:
            self.ftp.quit()
        except Exception:
            self.ftp.close()

    def listdir(self, path: str) -> list[RemoteFile]:
        lines: list[str] = []
        try:
            self.ftp.retrlines(f"LIST {path}", lines.append)
        except ftplib.error_perm as exc:
            if str(exc).startswith("550"):
                return []  # No such folder (no timelapse recorded yet).
            raise FtpsError(str(exc)) from exc
        except (OSError, EOFError, ftplib.Error) as exc:
            raise FtpsError(f"listing {path} failed: {exc}") from exc
        return parse_list(lines)

    def download(self, path: str, dest, offset: int, on_bytes: Callable[[int], None]) -> None:
        with open(dest, "ab" if offset else "wb") as fh:
            def write(chunk: bytes) -> None:
                fh.write(chunk)
                on_bytes(len(chunk))

            try:
                self.ftp.retrbinary(f"RETR {path}", write, blocksize=64 << 10, rest=offset or None)
            except (OSError, EOFError, ftplib.Error) as exc:
                raise FtpsError(f"download of {path} failed: {exc}") from exc

    def read(self, path: str, limit: int = 4 << 20) -> bytes:
        chunks: list[bytes] = []

        def keep(chunk: bytes) -> None:
            chunks.append(chunk)
            if sum(map(len, chunks)) > limit:
                raise FtpsError(f"{path} is larger than expected")

        try:
            self.ftp.retrbinary(f"RETR {path}", keep)
        except (OSError, EOFError, ftplib.Error) as exc:
            raise FtpsError(f"reading {path} failed: {exc}") from exc
        return b"".join(chunks)

    def delete(self, path: str) -> None:
        try:
            self.ftp.delete(path)
        except (OSError, EOFError, ftplib.Error) as exc:
            raise FtpsError(f"deleting {path} failed: {exc}") from exc
