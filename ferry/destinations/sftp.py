"""SFTP: any SSH server (a NAS with SSH enabled, a VPS, a Raspberry Pi), with a password or a key.

The host key is pinned on first contact ("SHA256:..." like OpenSSH prints it); a server that
later presents another key is refused instead of trusted.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import io
import posixpath
import stat as statmod
import threading
from pathlib import Path
from typing import BinaryIO, Callable

from ..core.settings import Credential, DestinationConfig
from .base import CHUNK, Destination, DestinationError, RemoteObject

TIMEOUT = 20


def host_key_fingerprint(key) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def load_private_key(text: str, passphrase: str | None):
    import paramiko

    last: Exception | None = None
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(text.strip() + "\n"), password=passphrase or None)
        except paramiko.PasswordRequiredException as exc:
            raise DestinationError("the private key is encrypted; enter its passphrase") from exc
        except (paramiko.SSHException, ValueError) as exc:
            last = exc
    raise DestinationError(f"cannot read the private key ({last})")


class SftpDestination(Destination):
    kind = "sftp"
    credential_kinds = frozenset({"password", "ssh_key"})
    capabilities = frozenset({"range_read"})

    def __init__(self, cfg: DestinationConfig, credential: Credential | None = None):
        if not cfg.host:
            raise DestinationError("the SFTP server is not configured")
        if credential is None or not credential.username:
            raise DestinationError("SFTP needs a username with a password or a key")
        self.cfg, self.credential = cfg, credential
        self.port = cfg.port or 22
        # Relative to the login folder unless it starts with "/".
        self.root = cfg.base_path.replace("\\", "/").rstrip("/") or "."
        self._lock = threading.Lock()
        self._transport = None
        self._sftp = None

    def describe(self, key: str = "") -> str:
        where = posixpath.join(self.root, key) if key else self.root
        port = f":{self.port}" if self.port != 22 else ""
        return f"sftp://{self.credential.username}@{self.cfg.host}{port}/{where.lstrip('./') if where != '.' else ''}"

    # --- connection --------------------------------------------------------

    def _connect(self):
        import paramiko

        try:
            transport = paramiko.Transport((self.cfg.host, self.port))
            transport.banner_timeout = TIMEOUT
            transport.start_client(timeout=TIMEOUT)
        except (OSError, paramiko.SSHException) as exc:
            raise DestinationError(f"cannot reach {self.cfg.host}:{self.port} ({exc})") from exc
        fingerprint = host_key_fingerprint(transport.get_remote_server_key())
        if self.cfg.fingerprint and fingerprint != self.cfg.fingerprint:
            transport.close()
            raise DestinationError(f"the server's host key changed (now {fingerprint}); if it was reinstalled, "
                                   "clear the pinned key in the destination and test again")
        try:
            if self.credential.kind == "ssh_key":
                key = load_private_key(self.credential.private_key, self.credential.password)
                transport.auth_publickey(self.credential.username, key)
            else:
                transport.auth_password(self.credential.username, self.credential.password)
        except paramiko.AuthenticationException as exc:
            transport.close()
            raise DestinationError("login refused: check the username and password or key") from exc
        if not self.cfg.fingerprint:
            self.cfg.fingerprint = fingerprint
            self._learn(fingerprint=fingerprint)
        self._transport = transport
        self._sftp = paramiko.SFTPClient.from_transport(transport)
        return self._sftp

    def _client(self):
        if self._sftp is None or self._transport is None or not self._transport.is_active():
            return self._connect()
        return self._sftp

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
        self._transport = self._sftp = None

    def _path(self, key: str) -> str:
        return posixpath.join(self.root, *[p for p in key.split("/") if p])

    def _makedirs(self, sftp, folder: str) -> None:
        parts, current = [p for p in folder.split("/") if p], "/" if folder.startswith("/") else ""
        for part in parts:
            current = posixpath.join(current, part) if current else part
            try:
                if not statmod.S_ISDIR(sftp.stat(current).st_mode):
                    raise DestinationError(f"{current} exists and is not a folder")
            except FileNotFoundError:
                sftp.mkdir(current)

    # --- Destination ---------------------------------------------------------

    def _stat(self, key: str) -> RemoteObject | None:
        with self._lock:
            sftp = self._client()
            try:
                return RemoteObject(sftp.stat(self._path(key)).st_size)
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return None
                raise DestinationError(str(exc)) from exc

    def _put(self, local: Path, key: str, on_bytes: Callable[[int], None]) -> None:
        dest = self._path(key)
        partial = dest + ".partial"
        with self._lock:
            sftp = self._client()
            self._makedirs(sftp, posixpath.dirname(dest))
            with open(local, "rb") as src, sftp.open(partial, "wb") as out:
                out.set_pipelined(True)
                while chunk := src.read(CHUNK):
                    out.write(chunk)
                    on_bytes(len(chunk))
            try:
                sftp.posix_rename(partial, dest)  # atomic replace (OpenSSH extension)
            except OSError:
                try:
                    sftp.remove(dest)
                except OSError:
                    pass
                sftp.rename(partial, dest)

    def _open_read(self, key: str) -> BinaryIO:
        with self._lock:
            # No prefetch: it would pull the whole video when the browser wants one range.
            return self._client().open(self._path(key), "rb")

    def _test(self) -> None:
        with self._lock:
            sftp = self._client()
            self._makedirs(sftp, self.root)
            probe = posixpath.join(self.root, ".ferry-test")
            with sftp.open(probe, "w") as fh:
                fh.write("ok")
            sftp.remove(probe)
