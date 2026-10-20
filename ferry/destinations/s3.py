"""S3-compatible object storage: Amazon S3, Cloudflare R2, Backblaze B2, MinIO, Wasabi, Garage.

A small SigV4 client on httpx. Every request is signed over the SHA-256 of its body, so the
server rejects a part that arrives damaged; large files go up as multipart uploads, which only
become visible once completed (an aborted upload leaves nothing behind).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Callable
from urllib.parse import quote, urlsplit

import httpx

from ..core.settings import Credential, DestinationConfig
from .base import Destination, DestinationError, RemoteObject

PART_SIZE = 16 << 20  # S3 allows 5 MiB..5 GiB per part and 10 000 parts: 16 MiB covers 160 GB files
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
RETRIES = 4
TIMEOUT = httpx.Timeout(30.0, read=120.0, write=120.0)
PRESIGN_SECONDS = 3600


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _xml_text(body: bytes, tag: str) -> str | None:
    root = ET.fromstring(body)
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == tag:
            return el.text
    return None


class SigV4:
    def __init__(self, key_id: str, secret: str, region: str):
        self.key_id, self.secret, self.region = key_id, secret, region or "us-east-1"

    def _signing_key(self, day: str) -> bytes:
        key = _hmac(f"AWS4{self.secret}".encode(), day)
        for part in (self.region, "s3", "aws4_request"):
            key = _hmac(key, part)
        return key

    @staticmethod
    def _canonical_query(params: dict[str, str]) -> str:
        return "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(params.items()))

    def sign(self, method: str, url: str, params: dict[str, str], headers: dict[str, str], payload_hash: str,
             now: dt.datetime | None = None) -> dict[str, str]:
        now = now or dt.datetime.now(dt.timezone.utc)
        stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        parts = urlsplit(url)
        headers = {**headers, "host": parts.netloc, "x-amz-date": stamp, "x-amz-content-sha256": payload_hash}
        names = sorted(k.lower() for k in headers)
        lower = {k.lower(): str(v).strip() for k, v in headers.items()}
        canonical = "\n".join([method, parts.path or "/", self._canonical_query(params),
                               "".join(f"{n}:{lower[n]}\n" for n in names), ";".join(names), payload_hash])
        scope = f"{day}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        signature = hmac.new(self._signing_key(day), to_sign.encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.key_id}/{scope}, "
                                    f"SignedHeaders={';'.join(names)}, Signature={signature}")
        return headers

    def presign(self, url: str, expires: int, now: dt.datetime | None = None) -> str:
        now = now or dt.datetime.now(dt.timezone.utc)
        stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        parts = urlsplit(url)
        scope = f"{day}/{self.region}/s3/aws4_request"
        params = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{self.key_id}/{scope}",
                  "X-Amz-Date": stamp, "X-Amz-Expires": str(expires), "X-Amz-SignedHeaders": "host"}
        canonical = "\n".join(["GET", parts.path, self._canonical_query(params), f"host:{parts.netloc}\n", "host",
                               "UNSIGNED-PAYLOAD"])
        to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        params["X-Amz-Signature"] = hmac.new(self._signing_key(day), to_sign.encode(), hashlib.sha256).hexdigest()
        return f"{url}?{self._canonical_query(params)}"


class S3Destination(Destination):
    kind = "s3"
    credential_kinds = frozenset({"access_key"})
    capabilities = frozenset({"range_read", "presigned_url"})

    def __init__(self, cfg: DestinationConfig, credential: Credential | None = None):
        if not cfg.bucket:
            raise DestinationError("the bucket is not configured")
        if credential is None or not (credential.key_id and credential.secret):
            raise DestinationError("S3 destinations need an access key")
        endpoint = (cfg.endpoint or f"https://s3.{cfg.region or 'us-east-1'}.amazonaws.com").rstrip("/")
        if not endpoint.startswith(("https://", "http://")):
            endpoint = f"https://{endpoint}"
        self.endpoint, self.bucket = endpoint, cfg.bucket
        self.prefix = "/".join(p for p in cfg.base_path.replace("\\", "/").split("/") if p)
        self.signer = SigV4(credential.key_id, credential.secret, cfg.region or ("auto" if ".r2." in endpoint else ""))

    # --- plumbing ----------------------------------------------------------

    def _object_key(self, key: str) -> str:
        return "/".join(p for p in [self.prefix, *key.split("/")] if p)

    def _url(self, object_key: str = "") -> str:
        # Path-style addressing: works for every S3-compatible service, no per-bucket DNS.
        path = f"/{quote(self.bucket, safe='')}"
        if object_key:
            path += "/" + quote(object_key, safe="/-_.~")
        return self.endpoint + path

    def describe(self, key: str = "") -> str:
        host = urlsplit(self.endpoint).netloc
        return f"s3://{host}/{self.bucket}/{self._object_key(key)}".rstrip("/")

    def _request(self, client: httpx.Client, method: str, object_key: str = "", params: dict | None = None,
                 body: bytes = b"", headers: dict | None = None, ok=(200,)) -> httpx.Response:
        params = {k: str(v) for k, v in (params or {}).items()}
        payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        for attempt in range(1, RETRIES + 1):
            signed = self.signer.sign(method, self._url(object_key), params, dict(headers or {}), payload_hash)
            try:
                resp = client.request(method, self._url(object_key), params=params, content=body or None, headers=signed)
            except httpx.HTTPError as exc:
                if attempt == RETRIES:
                    raise DestinationError(f"{method} {self.describe()}: {exc!r}") from exc
            else:
                if resp.status_code in ok:
                    return resp
                if resp.status_code not in (429, 500, 502, 503, 504) or attempt == RETRIES:
                    code = _xml_text(resp.content, "Code") if resp.content.startswith(b"<") else None
                    raise DestinationError(f"{method} {object_key or self.bucket}: HTTP {resp.status_code}"
                                           + (f" {code}" if code else ""))
            time.sleep(min(2 ** attempt, 20))  # throttled or a transient server error: back off
        raise DestinationError("unreachable")

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=TIMEOUT)

    # --- Destination ---------------------------------------------------------

    def _stat(self, key: str) -> RemoteObject | None:
        with self._client() as client:
            resp = self._request(client, "HEAD", self._object_key(key), ok=(200, 404))
        if resp.status_code == 404:
            return None
        etag = resp.headers.get("etag", "").strip('"')
        # A single-part upload's ETag is the MD5 of the content; multipart ETags end in "-<parts>".
        return RemoteObject(int(resp.headers.get("content-length", 0)), md5=etag if etag and "-" not in etag else None)

    def _put(self, local: Path, key: str, on_bytes: Callable[[int], None]) -> None:
        object_key = self._object_key(key)
        size = local.stat().st_size
        with self._client() as client, open(local, "rb") as fh:
            if size <= PART_SIZE:
                body = fh.read()
                self._request(client, "PUT", object_key, body=body,
                              headers={"content-type": "application/octet-stream"})
                on_bytes(len(body))
                return
            resp = self._request(client, "POST", object_key, params={"uploads": ""})
            upload_id = _xml_text(resp.content, "UploadId")
            if not upload_id:
                raise DestinationError("the server did not start a multipart upload")
            parts: list[tuple[int, str]] = []
            try:
                number = 1
                while chunk := fh.read(PART_SIZE):
                    r = self._request(client, "PUT", object_key, params={"partNumber": number, "uploadId": upload_id},
                                      body=chunk)
                    parts.append((number, r.headers.get("etag", "")))
                    on_bytes(len(chunk))
                    number += 1
                xml = "".join(f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>" for n, e in parts)
                self._request(client, "POST", object_key, params={"uploadId": upload_id},
                              body=f"<CompleteMultipartUpload>{xml}</CompleteMultipartUpload>".encode(),
                              headers={"content-type": "application/xml"})
            except BaseException:
                try:
                    self._request(client, "DELETE", object_key, params={"uploadId": upload_id}, ok=(200, 204, 404))
                except Exception:
                    pass
                raise

    def _open_read(self, key: str) -> BinaryIO:
        raise NotImplementedError("S3 objects are read with ranged GETs (open_range)")

    async def open_range(self, key: str, start: int, end: int) -> AsyncIterator[bytes]:
        object_key = self._object_key(key)
        headers = self.signer.sign("GET", self._url(object_key), {}, {"range": f"bytes={start}-{end}"}, EMPTY_SHA256)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            async with client.stream("GET", self._url(object_key), headers=headers) as resp:
                if resp.status_code not in (200, 206):
                    raise DestinationError(f"GET {object_key}: HTTP {resp.status_code}")
                async for chunk in resp.aiter_bytes(1 << 20):
                    yield chunk

    def presigned_url(self, key: str, expires: int = PRESIGN_SECONDS) -> str:
        return self.signer.presign(self._url(self._object_key(key)), expires)

    def _test(self) -> None:
        probe = self._object_key(".ferry-test")
        with self._client() as client:
            self._request(client, "PUT", probe, body=b"ok")
            self._request(client, "DELETE", probe, ok=(200, 204))
