import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ferry.auth import AuthStore
from ferry.core.service import SyncService
from ferry.core.session import Session
from ferry.core.settings import Settings, SettingsStore
from ferry.core.store import Store
from ferry.destinations import DestinationError, resolve_destination
from ferry.web import MASK, create_app

# What GoPro Sync 0.1 wrote on the Raspberry Pi.
V1 = {
    "target": "smb",
    "smb": {"host": "SYNOLOGY01", "share": "photo", "username": "gopro-sync", "password": "pw", "base_path": "GoPro"},
    "local_path": "", "folder_template": "{year}/{date}", "delete_after_sync": False, "sleep_after_sync": True,
    "usb_enabled": True, "ble_enabled": False, "wifi_interfaces": ["wlan0"], "max_parallel_syncs": 4,
    "ble_cooldown_minutes": 45, "cameras": [{"address": "AA:BB:CC", "name": "HERO9"}],
}


def test_v1_settings_migrate_without_moving_files():
    s = Settings.model_validate(V1)
    assert s.version == 2
    (nas,) = s.destinations
    assert (nas.type, nas.host, nas.share, nas.base_path, nas.credentials) == ("smb", "SYNOLOGY01", "photo", "GoPro", "nas")
    assert s.credential("nas").password == "pw"
    assert s.defaults.destination == "nas"
    policy = s.effective("gopro", "gopro:AA:BB:CC")
    assert policy.key({"year": "2026", "date": "2026-10-20"} | dict.fromkeys(("month", "day", "camera", "provider",
                                                                              "device", "model"), "x"), "GX01.MP4") \
        == "2026/2026-10-20/GX01.MP4"
    assert s.provider("gopro").options == {"usb": True, "ble": False, "sleep_after_sync": True, "cooldown_minutes": 45}
    assert [(d.id, d.name, d.config) for d in s.devices] == [("gopro:AA:BB:CC", "HERO9", {"address": "AA:BB:CC"})]


def test_store_upgrades_file_once_and_keeps_a_backup(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(V1))
    store = SettingsStore(path)
    assert store.load().defaults.destination == "nas"
    assert json.loads(path.read_text())["version"] == 2
    assert json.loads((tmp_path / "settings.json.v1.bak").read_text()) == V1
    store.load()
    assert json.loads((tmp_path / "settings.json.v1.bak").read_text()) == V1


def _layered() -> Settings:
    return Settings.model_validate({
        "version": 2,
        "destinations": [{"id": "nas", "type": "smb", "host": "nas", "share": "photo", "credentials": "main"},
                         {"id": "usb", "type": "local", "path": "/mnt/usb"}],
        "credentials": [{"id": "main", "username": "ferry", "password": "a"},
                        {"id": "printer", "username": "printer", "password": "b"}],
        "defaults": {"destination": "nas", "path": "{provider}", "folder_template": "{year}/{date}"},
        "providers": {"gopro": {"sync": {"path": "Action/{provider}", "delete_after_sync": True}}},
        "devices": [{"id": "gopro:1", "provider": "gopro", "name": "Helmet",
                     "sync": {"folder_template": "{device}/{year}", "credentials": "printer"}},
                    {"id": "gopro:2", "provider": "gopro", "sync": {"destination": "usb", "delete_after_sync": False}}],
    })


def test_layers_resolve_field_by_field_with_origin():
    s = _layered()
    p = s.effective("gopro", "gopro:1")
    assert (p.destination, p.credentials, p.path, p.folder_template, p.delete_after_sync) == \
        ("nas", "printer", "Action/{provider}", "{device}/{year}", True)
    assert p.origin == {"destination": "global", "credentials": "device", "path": "provider",
                        "folder_template": "device", "delete_after_sync": "provider", "mirrors": "global"}
    p2 = s.effective("gopro", "gopro:2")
    assert (p2.destination, p2.delete_after_sync, p2.origin["delete_after_sync"]) == ("usb", False, "device")
    other = s.effective("bambu")
    assert (other.path, other.delete_after_sync) == ("{provider}", False)


def test_credentials_override_reaches_the_destination():
    s = _layered()
    assert resolve_destination(s, "nas").username == "ferry"
    assert resolve_destination(s, "nas", "printer").username == "printer"
    with pytest.raises(DestinationError, match="cannot use password"):
        resolve_destination(s, "usb", "printer")
    with pytest.raises(DestinationError, match="no destination"):
        resolve_destination(Settings(), "")


@pytest.mark.parametrize("change, message", [
    ({"defaults": {"destination": "nope"}}, "unknown destination"),
    ({"defaults": {"folder_template": "{year}/{camra}"}}, "unknown placeholder"),
    ({"defaults": {"path": "../outside"}}, "'..'"),
    ({"devices": [{"id": "x", "provider": "gopro", "sync": {"credentials": "nope"}}]}, "unknown credentials"),
    ({"destinations": [{"id": "a"}, {"id": "a"}]}, "duplicate destination"),
])
def test_invalid_settings_are_rejected(change, message):
    with pytest.raises(ValueError, match=message):
        Settings.model_validate({"version": 2, **change})


def test_store_migrates_an_old_database(tmp_path):
    db = tmp_path / "state.db"
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE files (camera TEXT NOT NULL, path TEXT NOT NULL, created INTEGER NOT NULL, size INTEGER NOT NULL,
            dest TEXT NOT NULL, synced_at INTEGER NOT NULL, deleted_from_camera INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (camera, path, created, size));
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at INTEGER NOT NULL, finished_at INTEGER,
            mode TEXT NOT NULL, camera TEXT, status TEXT NOT NULL, files INTEGER NOT NULL DEFAULT 0,
            bytes INTEGER NOT NULL DEFAULT 0, error TEXT);
        INSERT INTO files VALUES ('C344', '100GOPRO/GX01.MP4', 1, 10, '2026/GX01.MP4', 1, 0);
    """)
    old.commit()
    old.close()
    store = Store(db)
    assert store.is_synced("C344", "100GOPRO/GX01.MP4", 1, 10)
    (item,), total = store.library()
    assert (item["provider"], item["dest_id"], total) == ("gopro", None, 1)
    Store(db)  # Idempotent.


def test_library_filters_by_kind(tmp_path):
    store = Store(tmp_path / "state.db")
    store.mark_synced("C1", "100GOPRO/GX01.MP4", 3, 10, "a/GX01.MP4")
    store.mark_synced("C1", "100GOPRO/GOPR02.JPG", 2, 10, "a/GOPR02.JPG")
    store.mark_synced("bambu:P1", "timelapse/video_1.avi", 1, 10, "b/video_1.avi", provider="bambu")
    names = lambda items: [i["path"].rsplit("/", 1)[-1] for i in items]
    videos, total = store.library(kind="video")
    assert (names(videos), total) == (["GX01.MP4", "video_1.avi"], 2)
    photos, total = store.library(kind="photo")
    assert (names(photos), total) == (["GOPR02.JPG"], 1)
    assert names(store.library(provider="bambu", kind="video")[0]) == ["video_1.avi"]
    assert store.library(kind="bogus")[1] == 3


@pytest.fixture
def client(tmp_path):
    lib = tmp_path / "nas"
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(Settings.model_validate({
        "version": 2,
        "destinations": [{"id": "nas", "type": "local", "path": str(lib)}],
        "credentials": [{"id": "c", "username": "u", "password": "secret"}],
        "defaults": {"destination": "nas"},
        "devices": [{"id": "gopro:AA", "provider": "gopro", "name": "Helmet", "config": {"address": "AA"}}],
    }))
    service = SyncService(settings_store, Store(tmp_path / "state.db"), tmp_path)
    with TestClient(create_app(service, AuthStore(tmp_path / "auth.json"))) as c:
        c.post("/api/auth/setup", json={"password": "long-enough-pw"})
        c.service = service
        yield c


def test_settings_api_masks_and_keeps_secrets(client):
    data = client.get("/api/settings").json()
    assert data["credentials"][0]["password"] == MASK
    data["defaults"]["path"] = "Media/{provider}"
    data["devices"] = []
    saved = client.put("/api/settings", json=data).json()
    assert saved["defaults"]["path"] == "Media/{provider}"
    assert [d["id"] for d in saved["devices"]] == ["gopro:AA"]
    assert client.service.settings.credential("c").password == "secret"
    bad = client.put("/api/settings", json={**data, "defaults": {**data["defaults"], "folder_template": "{nope}"}})
    assert bad.status_code == 422 and "unknown placeholder" in bad.json()["detail"]


def test_device_overrides_and_effective_preview(client):
    r = client.put("/api/devices/gopro:AA", json={"name": "Helmet cam", "sync": {"path": "Bike", "folder_template": "{device}"}})
    assert r.status_code == 200
    eff = client.get("/api/effective", params={"provider": "gopro", "device": "gopro:AA"}).json()
    assert eff["example"] == "Bike/Helmet cam/EXAMPLE.MP4"
    assert eff["origin"]["path"] == "device" and eff["origin"]["destination"] == "global"
    assert eff["location"].endswith("Bike/Helmet cam/EXAMPLE.MP4")
    assert client.put("/api/devices/nope", json={"name": "x"}).status_code == 404
    assert client.put("/api/devices/gopro:AA", json={"sync": {"destination": "missing"}}).status_code == 422
    assert client.post("/api/devices", json={"provider": "gopro", "config": {}}).status_code == 422
    assert client.delete("/api/devices/gopro:AA").status_code == 200
    assert client.service.settings.devices == []


def test_destination_test_endpoint(client):
    r = client.post("/api/destinations/nas/test")
    assert r.status_code == 200 and r.json()["target"].endswith("nas")
    assert client.post("/api/destinations/missing/test").status_code == 400


def test_gopro_identify_registers_usb_cameras_and_reuses_paired_ones(client):
    gopro = client.service.provider("gopro")
    usb = Session("usb:x", "usb", "x", "USB", provider="gopro")
    info = gopro._identify(usb, {"serial_number": "C999", "model_name": "HERO12 Black"}, "x")
    assert (usb.device, info.id, info.model, info.name) == ("gopro:C999", "C999", "HERO12 Black", "HERO12 Black")
    paired = Session("ble:AA", "wifi", "AA", "Helmet", provider="gopro", device="gopro:AA")
    info = gopro._identify(paired, {"serial_number": "C344", "model_name": "HERO9 Black"}, "AA")
    assert (paired.device, info.name) == ("gopro:AA", "Helmet")
    # The same camera plugged in over USB later maps to the paired entry through its serial.
    again = Session("usb:y", "usb", "y", "USB", provider="gopro")
    gopro._identify(again, {"serial_number": "C344", "model_name": "HERO9 Black"}, "y")
    assert again.device == "gopro:AA"
    assert {d.id for d in client.service.settings.devices} == {"gopro:AA", "gopro:C999"}
