"""Google Drive, through the user's own OAuth client and the device flow.

The device flow ("TVs and limited input devices" client) needs no redirect back to Ferry, so it
works from a LAN address over plain http. Its scope is drive.file: Ferry sees only the folders and
files it created, starting with its root folder in My Drive.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Callable

import httpx

from ..core.settings import Credential, DestinationConfig
from .base import Destination, DestinationError, RemoteObject

TOKEN_URL = "https://oauth2.googleapis.com/token"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER = "application/vnd.google-apps.folder"
CHUNK = 8 << 20  # resumable chunks must be multiples of 256 KiB
RETRIES = 5
TIMEOUT = httpx.Timeout(30.0, read=120.0, write=120.0)

_tokens: dict[str, tuple[str, float]] = {}  # refresh-token digest -> (access token, expiry)
_tokens_lock = threading.Lock()


class OAuthPending(Exception):
    pass


# --- OAuth device flow -------------------------------------------------------


def start_device_flow(client_id: str, transport: httpx.BaseTransport | None = None) -> dict:
    """Returns user_code, verification_url, device_code, interval, expires_in."""
    with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
        resp = client.post(DEVICE_URL, data={"client_id": client_id, "scope": SCOPE})
    if resp.status_code != 200:
        raise DestinationError(f"Google refused the device sign-in: {resp.text[:200]}")
    return resp.json()


def poll_device_flow(client_id: str, client_secret: str, device_code: str,
                     transport: httpx.BaseTransport | None = None) -> str:
    """The refresh token once the user has approved; raises OAuthPending until then."""
    with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
        resp = client.post(TOKEN_URL, data={"client_id": client_id, "client_secret": client_secret,
                                            "device_code": device_code,
                                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    body = resp.json() if resp.content else {}
    if resp.status_code == 200 and body.get("refresh_token"):
        return body["refresh_token"]
    error = body.get("error", f"HTTP {resp.status_code}")
    if error in ("authorization_pending", "slow_down"):
        raise OAuthPending(error)
    raise DestinationError({"access_denied": "access was denied in the Google prompt",
                            "expired_token": "the code expired; start again"}.get(error, error))


def access_token(credential: Credential, transport: httpx.BaseTransport | None = None) -> str:
    if not credential.refresh_token:
        raise DestinationError("this Google account is not connected yet")
    digest = hashlib.sha256(credential.refresh_token.encode()).hexdigest()
    with _tokens_lock:
        token, expiry = _tokens.get(digest, ("", 0.0))
        if token and expiry > time.time() + 60:
            return token
    with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
        resp = client.post(TOKEN_URL, data={"client_id": credential.client_id, "client_secret": credential.client_secret,
                                            "refresh_token": credential.refresh_token, "grant_type": "refresh_token"})
    if resp.status_code != 200:
        raise DestinationError("Google no longer accepts this sign-in (revoked or expired); connect the account again")
    body = resp.json()
    with _tokens_lock:
        _tokens[digest] = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
    return body["access_token"]


def account_email(credential: Credential, transport: httpx.BaseTransport | None = None) -> str:
    """The signed-in user's address, shown next to the credentials."""
    token = access_token(credential, transport)
    with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
        resp = client.get(f"{API}/about", params={"fields": "user(emailAddress)"},
                          headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        raise DestinationError(f"Google Drive: HTTP {resp.status_code}")
    return resp.json().get("user", {}).get("emailAddress", "")


def _quote(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


# --- destination -----------------------------------------------------------------


class GoogleDriveDestination(Destination):
    kind = "gdrive"
    credential_kinds = frozenset({"oauth_google"})
    capabilities = frozenset({"range_read"})

    def __init__(self, cfg: DestinationConfig, credential: Credential | None = None,
                 transport: httpx.BaseTransport | None = None, async_transport: httpx.AsyncBaseTransport | None = None):
        if credential is None:
            raise DestinationError("Google Drive destinations need a connected Google account")
        self.credential = credential
        self.root_path = [p for p in (cfg.base_path or "Ferry").replace("\\", "/").split("/") if p]
        self._transport, self._async_transport = transport, async_transport
        self._folders: dict[str, str] = {}  # "Ferry/GoPro/2026" -> folder id

    def describe(self, key: str = "") -> str:
        account = f"{self.credential.account}:" if self.credential.account else ""
        return f"drive://{account}" + "/".join([*self.root_path, *[p for p in key.split("/") if p]])

    # --- http --------------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=TIMEOUT, transport=self._transport)

    def _request(self, client: httpx.Client, method: str, url: str, ok=(200,), headers: dict | None = None,
                 **kwargs) -> httpx.Response:
        for attempt in range(1, RETRIES + 1):
            headers = {**(headers or {}), "Authorization": f"Bearer {access_token(self.credential, self._transport)}"}
            try:
                resp = client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                if attempt == RETRIES:
                    raise DestinationError(f"Google Drive: {exc!r}") from exc
            else:
                if resp.status_code in ok:
                    return resp
                limited = resp.status_code == 403 and b"rateLimitExceeded" in resp.content
                if not (resp.status_code in (429, 500, 502, 503, 504) or limited) or attempt == RETRIES:
                    try:
                        message = resp.json()["error"]["message"]
                    except Exception:
                        message = resp.text[:200]
                    raise DestinationError(f"Google Drive: HTTP {resp.status_code} {message}")
            time.sleep(min(2 ** attempt, 30))
        raise DestinationError("unreachable")

    def _find(self, client: httpx.Client, parent: str, name: str, folder: bool) -> dict | None:
        q = f"name = '{_quote(name)}' and '{parent}' in parents and trashed = false"
        q += f" and mimeType {'=' if folder else '!='} '{FOLDER}'"
        resp = self._request(client, "GET", f"{API}/files", params={
            "q": q, "spaces": "drive", "fields": "files(id,name,size,md5Checksum)", "pageSize": 10})
        files = resp.json().get("files", [])
        return files[0] if files else None

    def _folder(self, client: httpx.Client, parts: list[str], create: bool) -> str | None:
        parent, path = "root", ""
        for part in parts:
            path = f"{path}/{part}" if path else part
            if path not in self._folders:
                found = self._find(client, parent, part, folder=True)
                if found is None:
                    if not create:
                        return None
                    resp = self._request(client, "POST", f"{API}/files", params={"fields": "id"},
                                         json={"name": part, "mimeType": FOLDER, "parents": [parent]})
                    found = resp.json()
                self._folders[path] = found["id"]
            parent = self._folders[path]
        return parent

    def _locate(self, client: httpx.Client, key: str, create: bool = False) -> tuple[str | None, str]:
        *dirs, name = [p for p in key.split("/") if p]
        return self._folder(client, [*self.root_path, *dirs], create), name

    def _file(self, client: httpx.Client, key: str) -> dict | None:
        parent, name = self._locate(client, key)
        return self._find(client, parent, name, folder=False) if parent else None

    # --- Destination ---------------------------------------------------------

    def _stat(self, key: str) -> RemoteObject | None:
        with self._client() as client:
            found = self._file(client, key)
        if found is None:
            return None
        return RemoteObject(int(found.get("size", 0)), md5=found.get("md5Checksum"))

    def _put(self, local: Path, key: str, on_bytes: Callable[[int], None]) -> None:
        size = local.stat().st_size
        md5 = hashlib.md5()
        with self._client() as client:
            parent, name = self._locate(client, key, create=True)
            existing = self._find(client, parent, name, folder=False)
            # A resumable session: the file only appears in Drive once the last byte is in.
            start = self._request(client, "POST", UPLOAD, params={"uploadType": "resumable", "fields": "id,size,md5Checksum"},
                                  headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "application/octet-stream"},
                                  json={"name": name, "parents": [parent]})
            session = start.headers["location"]
            result: dict = {}
            with open(local, "rb") as fh:
                offset = 0
                while offset < size or size == 0:
                    chunk = fh.read(CHUNK)
                    md5.update(chunk)
                    end = offset + len(chunk) - 1
                    rng = f"bytes {offset}-{end}/{size}" if chunk else f"bytes */{size}"
                    resp = self._request(client, "PUT", session, ok=(200, 201, 308), content=chunk,
                                         headers={"Content-Range": rng})
                    offset += len(chunk)
                    on_bytes(len(chunk))
                    if resp.status_code in (200, 201):
                        result = resp.json()
                        break
            if result.get("md5Checksum") and result["md5Checksum"] != md5.hexdigest():
                self._request(client, "DELETE", f"{API}/files/{result['id']}", ok=(200, 204, 404))
                raise DestinationError(f"{key}: Google Drive stored different bytes (checksum mismatch)")
            if existing is not None:
                # Replace: the old copy goes only after the new one is complete.
                self._request(client, "DELETE", f"{API}/files/{existing['id']}", ok=(200, 204, 404))

    def _open_read(self, key: str) -> BinaryIO:
        raise NotImplementedError("Drive files are read with ranged GETs (open_range)")

    async def open_range(self, key: str, start: int, end: int) -> AsyncIterator[bytes]:
        def locate() -> str:
            with self._client() as client:
                found = self._file(client, key)
            if found is None:
                raise DestinationError(f"{key} is not in Google Drive")
            return found["id"]

        file_id = await asyncio.to_thread(locate)
        token = await asyncio.to_thread(access_token, self.credential, self._transport)
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._async_transport) as client:
            async with client.stream("GET", f"{API}/files/{file_id}", params={"alt": "media"},
                                     headers={"Authorization": f"Bearer {token}", "Range": f"bytes={start}-{end}"}) as resp:
                if resp.status_code not in (200, 206):
                    raise DestinationError(f"Google Drive: HTTP {resp.status_code}")
                async for chunk in resp.aiter_bytes(1 << 20):
                    yield chunk

    def _test(self) -> None:
        with self._client() as client:
            parent = self._folder(client, self.root_path, create=True)
            resp = self._request(client, "POST", f"{API}/files", params={"fields": "id"},
                                 json={"name": ".ferry-test", "parents": [parent]})
            self._request(client, "DELETE", f"{API}/files/{resp.json()['id']}", ok=(200, 204))

