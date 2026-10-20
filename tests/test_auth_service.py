import asyncio

import pytest
from fastapi.testclient import TestClient

from gopro_sync.auth import AuthStore, hash_password, verify_password
from gopro_sync.config import Settings, SettingsStore
from gopro_sync.engine import RunResult
from gopro_sync.service import Session, SyncService
from gopro_sync.store import Store
from gopro_sync.web import create_app


def test_password_hash_roundtrip():
    stored = hash_password("correct horse")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)


def test_tokens_expire_with_secret_rotation(tmp_path):
    auth = AuthStore(tmp_path / "auth.json")
    auth.set_password("first-password")
    token = auth.issue()
    assert auth.valid(token)
    assert not auth.valid(token[:-2] + "xx")
    auth.set_password("second-password")
    assert not auth.valid(token)


def test_settings_upgrade_from_single_radio():
    assert Settings.model_validate({"wifi_interface": "wlan1"}).wifi_interfaces == ["wlan1"]


@pytest.fixture
def service(tmp_path):
    return SyncService(SettingsStore(tmp_path / "settings.json"), Store(tmp_path / "state.db"), tmp_path)


def test_api_requires_setup_then_session(service, tmp_path):
    auth = AuthStore(tmp_path / "auth.json")
    with TestClient(create_app(service, auth)) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/auth").json() == {"configured": False, "authenticated": False,
                                                    "version": client.get("/healthz").json()["version"]}
        assert client.post("/api/auth/setup", json={"password": "short"}).status_code == 422
        assert client.post("/api/auth/setup", json={"password": "long-enough-pw"}).status_code == 200
        assert client.get("/api/status").status_code == 200  # Cookie set by setup.
        assert client.post("/api/auth/setup", json={"password": "another-password"}).status_code == 409

        client.cookies.clear()
        assert client.get("/api/settings").status_code == 401
        assert client.post("/api/auth/login", json={"password": "long-enough-pw"}).status_code == 200
        assert client.get("/api/settings").status_code == 200


async def test_sessions_run_in_parallel_and_are_recorded(service):
    release = asyncio.Event()
    started = []

    def flow(name):
        async def run(session: Session):
            started.append(name)
            await release.wait()
            return f"HERO {name}", RunResult(files=1, bytes=10, deleted=0, skipped_existing=0)
        return run

    service._start(Session("usb:a", "usb", "a", "A"), flow("A"))
    service._start(Session("usb:b", "usb", "b", "B"), flow("B"))
    await asyncio.sleep(0.05)
    assert sorted(started) == ["A", "B"]
    assert service.snapshot()["state"] == "syncing"
    assert len(service.snapshot()["sessions"]) == 2

    release.set()
    await asyncio.sleep(0.05)
    assert service.sessions == {}
    runs = service.store.recent_runs()
    assert [r["status"] for r in runs] == ["ok", "ok"]
    assert {r["camera"] for r in runs} == {"HERO A", "HERO B"}


async def test_cancel_one_session_keeps_the_other(service):
    gate = asyncio.Event()

    async def slow(session):
        await gate.wait()
        return "cam", RunResult(0, 0, 0, 0)

    service._start(Session("usb:a", "usb", "a", "A"), slow)
    service._start(Session("ble:b", "wifi", "b", "B", radio="wlan1"), slow)
    service._radios_in_use.add("wlan1")
    await asyncio.sleep(0.01)
    assert service.cancel("ble:b") == 1
    await asyncio.sleep(0.05)
    assert list(service.sessions) == ["usb:a"]
    assert "wlan1" not in service._radios_in_use
    gate.set()
    await asyncio.sleep(0.05)
    statuses = sorted(r["status"] for r in service.store.recent_runs())
    assert statuses == ["cancelled", "ok"]


def test_library_lists_and_streams_ranges(service, tmp_path):
    media_root = tmp_path / "nas"
    (media_root / "2026/2026-10-20").mkdir(parents=True)
    (media_root / "2026/2026-10-20/GX010001.MP4").write_bytes(bytes(range(256)) * 4)
    settings = service.settings
    settings.target, settings.local_path = "local", str(media_root)
    service.settings_store.save(settings)
    service.store.mark_synced("cam1", "100GOPRO/GX010001.MP4", 1760900000, 1024, "2026/2026-10-20/GX010001.MP4")

    auth = AuthStore(tmp_path / "auth.json")
    with TestClient(create_app(service, auth)) as client:
        client.post("/api/auth/setup", json={"password": "long-enough-pw"})
        lib = client.get("/api/library").json()
        assert lib["total"] == 1
        item = lib["items"][0]
        assert (item["name"], item["thumb"]) == ("GX010001.MP4", False)
        assert client.get(f"/api/library/{item['id']}/thumb").status_code == 404

        full = client.get(f"/api/library/{item['id']}/media")
        assert full.status_code == 200 and len(full.content) == 1024
        assert full.headers["content-type"] == "video/mp4"

        part = client.get(f"/api/library/{item['id']}/media", headers={"Range": "bytes=256-259"})
        assert part.status_code == 206
        assert part.headers["content-range"] == "bytes 256-259/1024"
        assert part.content == bytes([0, 1, 2, 3])

        tail = client.get(f"/api/library/{item['id']}/media", headers={"Range": "bytes=-2"})
        assert tail.content == bytes([254, 255])
        assert client.get(f"/api/library/{item['id']}/media", headers={"Range": "bytes=5000-"}).status_code == 416
        assert client.get("/api/library/99/media").status_code == 404

        client.cookies.clear()
        assert client.get(f"/api/library/{item['id']}/media").status_code == 401
