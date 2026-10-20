"""Single admin password for the web UI, with signed session cookies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

COOKIE = "gopro_sync_session"
SESSION_TTL = 30 * 24 * 3600
MIN_PASSWORD = 8


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, _ = stored.split("$")
    except ValueError:
        return False
    return scheme == "scrypt" and hmac.compare_digest(hash_password(password, _unb64(salt)), stored)


class AuthStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        data = self._read()
        if "secret" not in data:
            data["secret"] = _b64(secrets.token_bytes(32))
            self._write(data)
        seed = os.environ.get("GOPRO_SYNC_PASSWORD")
        if seed and not data.get("password"):
            self.set_password(seed)

    def _read(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @property
    def configured(self) -> bool:
        return bool(self._read().get("password"))

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        with self._lock:
            data = self._read()
            data["password"] = hash_password(password)
            # Rotating the secret signs out every existing session.
            data["secret"] = _b64(secrets.token_bytes(32))
            self._write(data)

    def check_password(self, password: str) -> bool:
        stored = self._read().get("password", "")
        return bool(stored) and verify_password(password, stored)

    def issue(self) -> str:
        payload = _b64(json.dumps({"exp": int(time.time()) + SESSION_TTL}).encode())
        return f"{payload}.{self._sign(payload)}"

    def valid(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        payload, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._sign(payload)):
            return False
        try:
            return json.loads(_unb64(payload))["exp"] > time.time()
        except (ValueError, KeyError):
            return False

    def _sign(self, payload: str) -> str:
        key = _unb64(self._read()["secret"])
        return _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
