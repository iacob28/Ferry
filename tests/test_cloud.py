import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ferry.core.engine import DeviceInfo, SyncEngine
from ferry.core.media import MediaItem
from ferry.core.settings import Credential, DestinationConfig, Settings
from ferry.core.store import Store
from ferry.destinations import DestinationError, LocalDestination, build_destination
from ferry.destinations import gdrive
from ferry.destinations.s3 import EMPTY_SHA256, S3Destination, SigV4

AWS_DATE = dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc)
AWS_KEY, AWS_SECRET = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_sigv4_matches_the_aws_examples():
    # https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html (GET Object)
    signer = SigV4(AWS_KEY, AWS_SECRET, "us-east-1")
    headers = signer.sign("GET", "https://examplebucket.s3.amazonaws.com/test.txt", {}, {"range": "bytes=0-9"},
                          EMPTY_SHA256, now=AWS_DATE)
    assert headers["Authorization"].endswith(
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date,"
        " Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    # https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-query-string-auth.html
    url = signer.presign("https://examplebucket.s3.amazonaws.com/test.txt", 86400, now=AWS_DATE)
    assert "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404" in url


def test_s3_config_and_keys():
    cred = Credential(id="k", kind="access_key", key_id="a", secret="b")
    r2 = S3Destination(DestinationConfig(id="r2", type="s3", endpoint="abc.r2.cloudflarestorage.com", bucket="media",
                                         base_path="/Ferry/"), cred)
    assert r2.signer.region == "auto"
    assert r2.describe("GoPro/a b.MP4") == "s3://abc.r2.cloudflarestorage.com/media/Ferry/GoPro/a b.MP4"
    assert r2._url(r2._object_key("x/a b+c.mp4")) == "https://abc.r2.cloudflarestorage.com/media/Ferry/x/a%20b%2Bc.mp4"
    with pytest.raises(DestinationError, match="access key"):
        S3Destination(DestinationConfig(id="s", type="s3", bucket="b"), None)
    with pytest.raises(DestinationError, match="cannot use password"):
        build_destination(DestinationConfig(id="s", type="s3", bucket="b"), Credential(id="p", username="u"))


S3_ENDPOINT = os.environ.get("FERRY_TEST_S3")  # e.g. http://127.0.0.1:8333 (SeaweedFS/MinIO/Garage)


@pytest.mark.skipif(not S3_ENDPOINT, reason="set FERRY_TEST_S3=http://host:port with key ferrykey/ferrysecret123")
async def test_s3_conformance(tmp_path, monkeypatch):
    monkeypatch.setattr("ferry.destinations.s3.PART_SIZE", 5 << 20)
    cred = Credential(id="k", kind="access_key", key_id="ferrykey", secret="ferrysecret123")
    bucket = f"ferry-test-{uuid.uuid4().hex[:8]}"
    dest = S3Destination(DestinationConfig(id="s3", type="s3", endpoint=S3_ENDPOINT, bucket=bucket, base_path="t"), cred)
    with httpx.Client() as client:
        dest._request(client, "PUT")  # create the bucket
    await dest.test()
    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(12 << 20))
    assert await dest.stat("GoPro/big.bin") is None
    sent = []
    remote = await dest.put(big, "GoPro/big.bin", sent.append)
    assert remote.size == 12 << 20 and len(sent) == 3 and sum(sent) == 12 << 20
    small = tmp_path / "s.txt"
    small.write_bytes(b"hello")
    remote = await dest.put(small, "a b+c.txt", lambda n: None)
    assert remote.md5 == hashlib.md5(b"hello").hexdigest()
    got = b"".join([c async for c in dest.open_range("GoPro/big.bin", 5, 1004)])
    assert got == big.read_bytes()[5:1005]
    assert httpx.get(dest.presigned_url("a b+c.txt")).content == b"hello"


# --- Google Drive against a fake API ---------------------------------------------------


class FakeDrive:
    def __init__(self):
        self.files = {}
        self.uploads = {}
        self.polls = 0
        self.refreshes = 0
        self.corrupt = False

    def _new(self, **meta):
        fid = f"f{len(self.files) + 1}"
        self.files[fid] = {"id": fid, **meta}
        return self.files[fid]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        if url.netloc == "oauth2.googleapis.com":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if url.path == "/device/code":
                return httpx.Response(200, json={"device_code": "dc", "user_code": "ABCD-EFGH", "interval": 5,
                                                 "verification_url": "https://www.google.com/device", "expires_in": 1800})
            if form["grant_type"].endswith("device_code"):
                self.polls += 1
                if self.polls == 1:
                    return httpx.Response(428, json={"error": "authorization_pending"})
                return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
            self.refreshes += 1
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer at"
        path = url.path
        if path == "/drive/v3/about":
            return httpx.Response(200, json={"user": {"emailAddress": "me@example.com"}})
        if path == "/drive/v3/files" and request.method == "GET":
            m = re.match(r"name = '((?:[^'\\]|\\.)*)' and '(\w+)' in parents and trashed = false and mimeType (!?=) '([^']+)'", q["q"])
            name = re.sub(r"\\(.)", r"\1", m[1])
            hits = [f for f in self.files.values() if f["name"] == name and m[2] in f["parents"]
                    and ((f["mimeType"] == m[4]) == (m[3] == "="))]
            return httpx.Response(200, json={"files": [{k: v for k, v in f.items() if k != "data"} for f in hits]})
        if path == "/drive/v3/files" and request.method == "POST":
            meta = json.loads(request.content)
            return httpx.Response(200, json={"id": self._new(mimeType=meta.get("mimeType", "text/plain"), size="0", **{
                k: meta[k] for k in ("name", "parents")})["id"]})
        if path.startswith("/drive/v3/files/") and request.method == "DELETE":
            self.files.pop(path.rsplit("/", 1)[1], None)
            return httpx.Response(204)
        if path.startswith("/drive/v3/files/") and q.get("alt") == "media":
            data = self.files[path.rsplit("/", 1)[1]]["data"]
            start, end = map(int, request.headers["range"][6:].split("-"))
            return httpx.Response(206, content=data[start:end + 1])
        if path == "/upload/drive/v3/files" and q.get("uploadType") == "resumable":
            uid = f"u{len(self.uploads) + 1}"
            self.uploads[uid] = {**json.loads(request.content), "data": b"", "size": int(request.headers["x-upload-content-length"])}
            return httpx.Response(200, headers={"location": f"https://www.googleapis.com/upload/drive/v3/files?upload_id={uid}"})
        if path == "/upload/drive/v3/files" and "upload_id" in q:
            up = self.uploads[q["upload_id"]]
            up["data"] += request.content
            if len(up["data"]) < up["size"]:
                return httpx.Response(308)
            data = up["data"] + (b"!" if self.corrupt else b"")
            f = self._new(name=up["name"], parents=up["parents"], mimeType="application/octet-stream",
                          size=str(len(up["data"])), md5Checksum=hashlib.md5(data).hexdigest(), data=up["data"])
            return httpx.Response(200, json={k: f[k] for k in ("id", "size", "md5Checksum")})
        return httpx.Response(404, json={"error": {"message": f"fake: {request.method} {path}"}})


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    gdrive._tokens.clear()
    monkeypatch.setattr(gdrive, "CHUNK", 256 << 10)
    return fake, httpx.MockTransport(fake.handler)


def test_google_device_flow(drive):
    fake, transport = drive
    flow = gdrive.start_device_flow("client", transport)
    assert flow["user_code"] == "ABCD-EFGH"
    with pytest.raises(gdrive.OAuthPending):
        gdrive.poll_device_flow("client", "secret", flow["device_code"], transport)
    assert gdrive.poll_device_flow("client", "secret", flow["device_code"], transport) == "rt"
    cred = Credential(id="g", kind="oauth_google", client_id="client", client_secret="secret", refresh_token="rt")
    assert gdrive.account_email(cred, transport) == "me@example.com"
    gdrive.account_email(cred, transport)
    assert fake.refreshes == 1  # the access token is cached


async def test_google_drive_destination(drive, tmp_path):
    fake, transport = drive
    cred = Credential(id="g", kind="oauth_google", client_id="c", client_secret="s", refresh_token="rt", account="me@example.com")
    cfg = DestinationConfig(id="drive", type="gdrive", base_path="Ferry")
    dest = gdrive.GoogleDriveDestination(cfg, cred, transport, httpx.MockTransport(fake.handler))
    await dest.test()
    local = tmp_path / "clip.mp4"
    local.write_bytes(os.urandom(700 << 10))  # three resumable chunks
    assert await dest.stat("GoPro/2026/clip.mp4") is None
    sent = []
    remote = await dest.put(local, "GoPro/2026/clip.mp4", sent.append)
    assert remote.size == 700 << 10 and remote.md5 == hashlib.md5(local.read_bytes()).hexdigest() and len(sent) == 3
    folders = [f["name"] for f in fake.files.values() if f["mimeType"] == gdrive.FOLDER]
    assert folders == ["Ferry", "GoPro", "2026"]  # created once, then found
    await dest.put(local, "GoPro/2026/clip.mp4", lambda n: None)  # replace: old copy removed
    assert sum(f["name"] == "clip.mp4" for f in fake.files.values()) == 1
    got = b"".join([c async for c in dest.open_range("GoPro/2026/clip.mp4", 10, 19)])
    assert got == local.read_bytes()[10:20]
    assert dest.describe("GoPro/x.mp4") == "drive://me@example.com:Ferry/GoPro/x.mp4"
    fake.corrupt = True
    with pytest.raises(DestinationError, match="checksum"):
        await dest.put(local, "GoPro/bad.mp4", lambda n: None)
    assert not any(f["name"] == "bad.mp4" for f in fake.files.values())


# --- fan-out --------------------------------------------------------------------------


class OneFile:
    def __init__(self):
        self.deleted = []

    async def list_media(self):
        return [MediaItem("100GOPRO/GX01.MP4", "GX01.MP4", 1792506612, 4)]

    async def download(self, item, dest, on_bytes):
        dest.write_bytes(b"data")
        on_bytes(4)
        return 4

    async def delete(self, item):
        self.deleted.append(item.path)

    async def thumbnail(self, item):
        raise RuntimeError("none")


class Broken(LocalDestination):
    def _put(self, local, key, on_bytes):
        raise DestinationError("offsite is down")


async def test_mirrors_get_a_copy_and_gate_deletion(tmp_path):
    settings = Settings(defaults={"path": "", "folder_template": "{year}", "delete_after_sync": True})
    policy = settings.effective("gopro")
    nas = LocalDestination(DestinationConfig(id="nas", type="local", path=str(tmp_path / "nas")))
    off = LocalDestination(DestinationConfig(id="off", type="local", path=str(tmp_path / "off")))
    store = Store(tmp_path / "state.db")
    (tmp_path / "staging").mkdir()
    hero = DeviceInfo("gopro", "GoPro", "C1", "HERO9")

    def engine(*mirrors):
        return SyncEngine(store, policy, nas, tmp_path / "staging", lambda *a: None, lambda p: None, mirrors=list(mirrors))

    src = OneFile()
    broken = Broken(DestinationConfig(id="b", type="local", path=str(tmp_path / "b")))
    with pytest.raises(DestinationError, match="offsite is down"):
        await engine(broken).run(src, hero)
    assert src.deleted == [] and not store.is_synced("C1", "100GOPRO/GX01.MP4", 1792506612, 4)
    assert (tmp_path / "nas/2026/GX01.MP4").exists()  # the primary copy stays for the retry

    result = await engine(off).run(src, hero)
    assert (result.files, result.skipped_existing, result.deleted) == (1, 1, 1)
    assert (tmp_path / "off/2026/GX01.MP4").read_bytes() == b"data"
    assert src.deleted == ["100GOPRO/GX01.MP4"]


def test_mirror_references_are_checked():
    with pytest.raises(ValueError, match="unknown destination 'off'"):
        Settings.model_validate({"version": 2, "defaults": {"mirrors": ["off"]}})
