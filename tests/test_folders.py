import pytest
from fastapi.testclient import TestClient

from ferry import folders
from ferry.auth import AuthStore
from ferry.core.service import SyncService
from ferry.core.settings import SettingsStore
from ferry.core.store import Store
from ferry.web import create_app


def test_list_and_make_dirs(tmp_path):
    (tmp_path / "media" / "GoPro").mkdir(parents=True)
    (tmp_path / "media" / ".hidden").mkdir()
    (tmp_path / "media" / "clip.mp4").write_bytes(b"x")
    listing = folders.list_dirs(str(tmp_path / "media"))
    assert [d["name"] for d in listing["dirs"]] == ["GoPro"]
    assert listing["parent"] == str((tmp_path).resolve()) and listing["writable"]
    made = folders.make_dir(str(tmp_path / "media"), "Drone")
    assert made.endswith("/media/Drone")
    for bad in ("", "a/b", ".."):
        with pytest.raises(folders.FolderError):
            folders.make_dir(str(tmp_path), bad)
    with pytest.raises(folders.FolderError, match="already exists"):
        folders.make_dir(str(tmp_path / "media"), "Drone")
    with pytest.raises(folders.FolderError, match="does not exist"):
        folders.list_dirs(str(tmp_path / "nope"))
    with pytest.raises(folders.FolderError, match="absolute"):
        folders.list_dirs("relative/path")


def test_folder_api_needs_a_session(tmp_path):
    service = SyncService(SettingsStore(tmp_path / "settings.json"), Store(tmp_path / "state.db"), tmp_path)
    with TestClient(create_app(service, AuthStore(tmp_path / "auth.json"))) as client:
        assert client.get("/api/fs/dirs").status_code == 401
        client.post("/api/auth/setup", json={"password": "long-enough-pw"})
        r = client.get("/api/fs/dirs", params={"path": str(tmp_path)}).json()
        assert r["path"] == str(tmp_path.resolve()) and any(s["kind"] == "root" for s in r["shortcuts"])
        made = client.post("/api/fs/mkdir", json={"parent": str(tmp_path), "name": "Library"})
        assert made.status_code == 200 and (tmp_path / "Library").is_dir()
        assert client.post("/api/fs/mkdir", json={"parent": str(tmp_path), "name": "x/y"}).status_code == 400
