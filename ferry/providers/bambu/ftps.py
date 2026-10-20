"""The printer's SD card over FTPS: implicit TLS on port 990, user bblp, the LAN access code."""

from __future__ import annotations

import ftplib
import hashlib
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Callable

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


class ImplicitFtps(ftplib.FTP_TLS):
    """ftplib speaks explicit FTPS (AUTH TLS); Bambu printers want TLS from the first byte.

    The printer's certificate is self-signed, so it is pinned instead of verified: the first
    connection records its SHA-256 fingerprint and later ones must present the same certificate.
    """

    def __init__(self, fingerprint: str | None = None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(context=context, timeout=TIMEOUT)
        self.expected_fingerprint = fingerprint
        self.fingerprint: str | None = None

    def connect(self, host: str = "", port: int = PORT, timeout: float = TIMEOUT, source_address=None) -> str:
        self.host, self.port, self.timeout = host, port, timeout
        raw = socket.create_connection((host, port), timeout)
        self.sock = self.context.wrap_socket(raw, server_hostname=host)
        cert = self.sock.getpeercert(binary_form=True) or b""
        self.fingerprint = hashlib.sha256(cert).hexdigest()
        if self.expected_fingerprint and self.fingerprint != self.expected_fingerprint:
            self.sock.close()
            raise FtpsError("the printer presented a different TLS certificate than before; if it was reset "
                            "or replaced, set it up again")
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        # Bambu's server only accepts data connections that resume the control channel's TLS session.
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


class PrinterFiles:
    """Blocking client; the provider runs it in a worker thread."""

    def __init__(self, host: str, access_code: str, fingerprint: str | None = None, port: int = PORT):
        self.host, self.port = host, port
        self.access_code = access_code
        self.ftp = ImplicitFtps(fingerprint)

    def open(self) -> PrinterFiles:
        try:
            self.ftp.connect(self.host, self.port)
            self.ftp.login(USER, self.access_code)
            self.ftp.prot_p()
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
