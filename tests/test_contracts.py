import time

import pytest

from ferry.core.service import SyncService
from ferry.core.session import Session
from ferry.core.settings import SettingsStore, env
from ferry.core.store import Store
from ferry.destinations import LocalDestination
from ferry.providers.base import Provider
from ferry.providers.gopro import GoProProvider


class DummyProvider(Provider):
    kind = "dummy"
    label = "Dummy"

    def __init__(self, service):
        super().__init__(service)
        self.finished = []

    async def watch(self):
        pass

    def request_sync(self, mode, device):
        return 2 if mode in ("all", "dummy") else 0

    def snapshot(self):
        return {"dummy_devices": ["d1"]}

    def session_finished(self, session, ok):
        self.finished.append((session.id, ok))


@pytest.fixture
def service(tmp_path):
    return SyncService(SettingsStore(tmp_path / "settings.json"), Store(tmp_path / "state.db"), tmp_path,
                       providers=[GoProProvider, DummyProvider])


def test_snapshot_merges_providers_and_keeps_gopro_fields(service):
    snap = service.snapshot()
    assert [p["kind"] for p in snap["providers"]] == ["gopro", "dummy"]
    assert snap["dummy_devices"] == ["d1"]
    for key in ("usb_cameras", "ble_available", "ble_seen", "radios", "pairing", "totals"):
        assert key in snap


def test_request_sync_is_delegated(service):
    assert service.request_sync("dummy") == "sync requested for 2 device(s)"
    assert service.request_sync("usb").startswith("nothing to sync")


async def test_session_end_reaches_its_provider(service):
    async def flow(session):
        raise RuntimeError("boom")

    service.start_session(Session("dummy:1", "usb", "1", "One", provider="dummy"), flow)
    await service.sessions["dummy:1"].task
    assert service.provider("dummy").finished == [("dummy:1", False)]
    assert service.store.recent_runs()[0]["status"] == "failed"


def test_gopro_wifi_cooldown_after_session(service):
    gopro = service.provider("gopro")
    gopro.session_finished(Session("ble:AA", "wifi", "AA", "Cam", provider="gopro"), ok=False)
    assert 0 < gopro._ble_next_allowed["AA"] - time.time() <= 5 * 60
    gopro.session_finished(Session("usb:x", "usb", "x", "Cam", provider="gopro"), ok=True)
    assert "x" not in gopro._ble_next_allowed


def test_unknown_provider_is_reported(service):
    with pytest.raises(RuntimeError, match="not available"):
        service.provider("bambu")


def test_env_prefers_ferry_and_falls_back_to_legacy(monkeypatch):
    monkeypatch.delenv("FERRY_PORT", raising=False)
    monkeypatch.setenv("GOPRO_SYNC_PORT", "9000")
    assert env("PORT", "8780") == "9000"
    monkeypatch.setenv("FERRY_PORT", "9100")
    assert env("PORT", "8780") == "9100"
    monkeypatch.delenv("FERRY_PORT")
    monkeypatch.delenv("GOPRO_SYNC_PORT")
    assert env("PORT", "8780") == "8780"


async def test_local_destination_put_stat_and_range(tmp_path):
    dest = LocalDestination(str(tmp_path / "nas"))
    src = tmp_path / "file.bin"
    src.write_bytes(bytes(range(256)) * 8)
    assert await dest.stat("a/b/file.bin") is None
    remote = await dest.put(src, "a/b/file.bin", lambda n: None)
    assert remote.size == 2048
    assert not list((tmp_path / "nas").rglob("*.partial"))
    chunks = [c async for c in dest.open_range("a/b/file.bin", 254, 257)]
    assert b"".join(chunks) == bytes([254, 255, 0, 1])
    await dest.test()
