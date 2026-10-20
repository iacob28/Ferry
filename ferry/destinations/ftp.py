"""FTP, FTPES (explicit TLS) and FTPS (implicit TLS on 990), for NAS boxes and hosting accounts.

Plain FTP sends the password in the clear: use it only on a trusted LAN. With TLS, the server
certificate is pinned on first contact (see ferry.ftpclient).
"""

from __future__ import annotations

import asyncio
import ftplib
import io
import posixpath
import threading
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Callable

from ..core.settings import Credential, DestinationConfig
from ..ftpclient import TIMEOUT, CertificateChanged, ExplicitFtps, ImplicitFtps
from .base import CHUNK, Destination, DestinationError, RemoteObject

_ERRORS = (OSError, EOFError, ftplib.Error)


class _StopReading(Exception):
    pass


class FtpDestination(Destination):
    kind = "ftp"
    credential_kinds = frozenset({"password"})
    capabilities = frozenset({"range_read"})

    def __init__(self, cfg: DestinationConfig, credential: Credential | None = None):
        if not cfg.host:
            raise DestinationError("the FTP server is not configured")
        self.cfg, self.credential = cfg, credential
        self.port = cfg.port or (990 if cfg.ftp_tls == "implicit" else 21)
        self.root = "/" + "/".join(p for p in cfg.base_path.replace("\\", "/").split("/") if p)
        self._lock = threading.Lock()
        self._ftp: ftplib.FTP | None = None

    def describe(self, key: str = "") -> str:
        scheme = {"none": "ftp", "explicit": "ftpes", "implicit": "ftps"}[self.cfg.ftp_tls]
        default = 990 if self.cfg.ftp_tls == "implicit" else 21
        port = f":{self.port}" if self.port != default else ""
        return f"{scheme}://{self.cfg.host}{port}{posixpath.join(self.root, key) if key else self.root}"

    # --- connection --------------------------------------------------------

    def _open(self) -> ftplib.FTP:
        tls = self.cfg.ftp_tls
        ftp: ftplib.FTP = (ImplicitFtps(self.cfg.fingerprint) if tls == "implicit"
                           else ExplicitFtps(self.cfg.fingerprint) if tls == "explicit" else ftplib.FTP(timeout=TIMEOUT))
        user = self.credential.username if self.credential else "anonymous"
        password = self.credential.password if self.credential else ""
        try:
            ftp.connect(self.cfg.host, self.port, timeout=TIMEOUT)
            ftp.login(user, password)
            if tls != "none":
                ftp.prot_p()
            ftp.voidcmd("TYPE I")
        except CertificateChanged as exc:
            raise DestinationError(str(exc)) from exc
        except ftplib.error_perm as exc:
            raise DestinationError(f"login refused ({exc})") from exc
        except _ERRORS as exc:
            raise DestinationError(f"cannot reach {self.cfg.host}:{self.port} ({exc})") from exc
        fingerprint = getattr(ftp, "fingerprint", None)
        if fingerprint and not self.cfg.fingerprint:
            self.cfg.fingerprint = fingerprint
            self._learn(fingerprint=fingerprint)
        return ftp

    def _client(self) -> ftplib.FTP:
        if self._ftp is not None:
            try:
                self._ftp.voidcmd("NOOP")
                return self._ftp
            except _ERRORS:
                self._ftp.close()
        self._ftp = self._open()
        return self._ftp

    def close(self) -> None:
        if self._ftp is not None:
            try:
                self._ftp.quit()
            except _ERRORS:
                self._ftp.close()
            self._ftp = None

    def _path(self, key: str) -> str:
        return posixpath.join(self.root, *[p for p in key.split("/") if p])

    @staticmethod
    def _makedirs(ftp: ftplib.FTP, folder: str) -> None:
        current = ""
        for part in [p for p in folder.split("/") if p]:
            current += "/" + part
            try:
                ftp.mkd(current)
            except ftplib.error_perm as exc:
                if not str(exc).startswith("550"):  # 550: it exists already
                    raise

    def _size(self, ftp: ftplib.FTP, path: str) -> int | None:
        try:
            size = ftp.size(path)
        except ftplib.error_perm as exc:
            if str(exc).startswith(("550", "553")):
                return None
            raise DestinationError(str(exc)) from exc
        return int(size) if size is not None else None

    # --- Destination ---------------------------------------------------------

    def _stat(self, key: str) -> RemoteObject | None:
        with self._lock:
            size = self._size(self._client(), self._path(key))
        return RemoteObject(size) if size is not None else None

    def _put(self, local: Path, key: str, on_bytes: Callable[[int], None]) -> None:
        dest = self._path(key)
        partial = dest + ".partial"
        with self._lock:
            ftp = self._client()
            try:
                self._makedirs(ftp, posixpath.dirname(dest))
                with open(local, "rb") as src:
                    ftp.storbinary(f"STOR {partial}", src, blocksize=CHUNK, callback=lambda b: on_bytes(len(b)))
                if self._size(ftp, dest) is not None:
                    ftp.delete(dest)
                ftp.rename(partial, dest)
            except _ERRORS as exc:
                raise DestinationError(f"upload of {key} failed: {exc}") from exc

    def _open_read(self, key: str) -> BinaryIO:
        raise NotImplementedError("FTP files are read with REST + RETR (open_range)")

    async def open_range(self, key: str, start: int, end: int) -> AsyncIterator[bytes]:
        """Its own connection per read, so the library never waits behind an upload."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | Exception | None] = asyncio.Queue(maxsize=8)
        wanted = end - start + 1
        stop = threading.Event()

        def read() -> None:
            ftp = None
            try:
                ftp = self._open()
                left = wanted

                def take(block: bytes) -> None:
                    nonlocal left
                    if stop.is_set():
                        raise _StopReading
                    piece = block[:left]
                    left -= len(piece)
                    asyncio.run_coroutine_threadsafe(queue.put(piece), loop).result()
                    if left <= 0:
                        raise _StopReading

                ftp.retrbinary(f"RETR {self._path(key)}", take, blocksize=1 << 20, rest=start or None)
            except _StopReading:
                pass
            except Exception as exc:
                asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
            finally:
                if ftp is not None:
                    ftp.close()
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

        worker = loop.run_in_executor(None, read)
        try:
            while (item := await queue.get()) is not None:
                if isinstance(item, Exception):
                    raise DestinationError(f"reading {key} failed: {item}") from item
                yield item
        finally:
            stop.set()
            while not worker.done():  # let the worker hand over its last pieces and exit
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.01)

    def _test(self) -> None:
        with self._lock:
            ftp = self._client()
            try:
                self._makedirs(ftp, self.root)
                probe = posixpath.join(self.root, ".ferry-test")
                ftp.storbinary(f"STOR {probe}", io.BytesIO(b"ok"))
                ftp.delete(probe)
            except _ERRORS as exc:
                raise DestinationError(f"cannot write to {self.root}: {exc}") from exc
