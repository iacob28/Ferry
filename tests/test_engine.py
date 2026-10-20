from pathlib import Path

import pytest

from ferry.core.engine import DeviceInfo, SyncEngine, thumb_name
from ferry.core.media import MediaItem, SourceError
from ferry.core.settings import DestinationConfig, Settings
from ferry.core.store import Store
from ferry.destinations import LocalDestination

DAY = 1758902400  # 2025-09-26
HERO = DeviceInfo("gopro", "GoPro", "C344", "HERO9 Black")


class FakeSource:
    def __init__(self, files: dict[str, bytes], fail_once: set[str] = frozenset()):
        self.files = files
        self.fail_once = set(fail_once)
        self.deleted: list[str] = []

    async def list_media(self):
        return [MediaItem(f"100GOPRO/{name}", name, DAY, len(data)) for name, data in self.files.items()]

    async def download(self, media, dest: Path, on_bytes):
        data = self.files[media.name]
        offset = dest.stat().st_size if dest.exists() else 0
        with open(dest, "ab") as fh:
            if media.name in self.fail_once:
                self.fail_once.discard(media.name)
                half = data[offset: offset + len(data) // 2]
                fh.write(half)
                on_bytes(len(half))
                raise SourceError("link dropped")
            fh.write(data[offset:])
            on_bytes(len(data) - offset)
        return len(data)

    async def delete(self, item):
        self.deleted.append(item.path)

    async def thumbnail(self, item):
        if item.name == "BROKEN.MP4":
            raise SourceError("HTTP 500")
        return b"jpeg:" + item.path.encode()


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "state.db")
    library = tmp_path / "library"
    settings = Settings(destinations=[DestinationConfig(id="lib", type="local", path=str(library))],
                        defaults={"destination": "lib", "path": ""})
    logs = []

    def make(settings=settings, thumbs=None):
        return SyncEngine(store, settings.effective("gopro"), LocalDestination(settings.destinations[0]),
                          tmp_path / "staging", lambda level, msg: logs.append((level, msg)), lambda p: None, thumbs)

    (tmp_path / "staging").mkdir()
    return store, library, settings, make, logs


async def test_offloads_new_files_and_skips_known(env):
    store, library, _, make, _ = env
    cam = FakeSource({"GX010001.MP4": b"a" * 1000, "GX010002.MP4": b"b" * 500})
    result = await make().run(cam, HERO)
    assert (result.files, result.bytes) == (2, 1500)
    assert (library / "2025/2025-09-26/GX010001.MP4").read_bytes() == b"a" * 1000
    assert not list((library.parent / "staging").rglob("*.part"))
    # Second run: nothing new.
    assert (await make().run(cam, HERO)).files == 0
    assert store.totals() == {"files": 2, "bytes": 1500}


async def test_resumes_after_dropped_link(env):
    _, library, _, make, logs = env
    cam = FakeSource({"GX010003.MP4": bytes(range(256)) * 40}, fail_once={"GX010003.MP4"})
    result = await make().run(cam, HERO)
    assert result.files == 1
    assert (library / "2025/2025-09-26/GX010003.MP4").read_bytes() == bytes(range(256)) * 40
    assert any("retrying" in msg for _, msg in logs)


async def test_name_clash_with_different_file_gets_suffix(env):
    _, library, _, make, _ = env
    clash = library / "2025/2025-09-26/GX010001.MP4"
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"older file from before a card format")
    await make().run(FakeSource({"GX010001.MP4": b"new"}), HERO)
    assert clash.read_bytes().startswith(b"older")
    assert (library / f"2025/2025-09-26/GX010001_{DAY}.MP4").read_bytes() == b"new"


async def test_existing_identical_file_is_not_reuploaded(env):
    _, library, _, make, _ = env
    dest = library / "2025/2025-09-26/GX010001.MP4"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"same")
    result = await make().run(FakeSource({"GX010001.MP4": b"same"}), HERO)
    assert result.skipped_existing == 1


async def test_delete_only_when_enabled(env):
    _, _, settings, make, _ = env
    cam = FakeSource({"GX010001.MP4": b"x"})
    await make().run(cam, HERO)
    assert cam.deleted == []

    deleting = settings.model_copy(update={"defaults": settings.defaults.model_copy(update={"delete_after_sync": True})})
    cam2 = FakeSource({"GX010009.MP4": b"y"})
    result = await make(deleting).run(cam2, HERO)
    assert cam2.deleted == ["100GOPRO/GX010009.MP4"]
    assert result.deleted == 1


async def test_delete_switched_on_later_clears_files_synced_before(env):
    store, library, settings, make, logs = env
    cam = FakeSource({"GX010001.MP4": b"a", "GX010002.MP4": b"b"})
    await make().run(cam, HERO)
    (library / "2025/2025-09-26/GX010002.MP4").unlink()  # No longer on the target: keep it.

    deleting = settings.model_copy(update={"defaults": settings.defaults.model_copy(update={"delete_after_sync": True})})
    result = await make(deleting).run(cam, HERO)
    assert cam.deleted == ["100GOPRO/GX010001.MP4"]
    assert result.deleted == 1
    assert any("no longer on the target" in msg for _, msg in logs)

    cam.deleted.clear()  # A camera that failed to delete keeps listing the file; deleted ones are not retried.
    await make(deleting).run(cam, HERO)
    assert cam.deleted == []


class BrokenSource(FakeSource):
    """Some files can never be read (a corrupt file on the card)."""

    def __init__(self, files, broken):
        super().__init__(files)
        self.broken = set(broken)
        self.tries: dict[str, int] = {}

    async def download(self, media, dest, on_bytes):
        if media.name in self.broken:
            self.tries[media.name] = self.tries.get(media.name, 0) + 1
            raise SourceError("502")
        return await super().download(media, dest, on_bytes)


@pytest.fixture
def no_retry_wait(monkeypatch):
    async def instant(_):
        pass
    monkeypatch.setattr("ferry.core.engine.asyncio.sleep", instant)


async def test_a_broken_file_is_skipped_and_given_up_after_three_runs(env, no_retry_wait):
    store, library, _, make, logs = env
    cam = BrokenSource({"A.MP4": b"a", "BAD.MP4": b"b", "C.MP4": b"c"}, {"BAD.MP4"})
    for _ in range(3):
        result = await make().run(cam, HERO)
        assert "BAD.MP4" in (result.problem or "")
    assert (library / "2025/2025-09-26/A.MP4").exists() and (library / "2025/2025-09-26/C.MP4").exists()
    assert cam.tries["BAD.MP4"] == 3 * 4  # Every run retries it a few times.

    result = await make().run(cam, HERO)  # Given up: no longer downloaded or reported.
    assert cam.tries["BAD.MP4"] == 12 and result.problem is None
    assert any("not trying 1 file(s)" in msg for _, msg in logs)


async def test_files_failing_back_to_back_still_fail_the_run(env, no_retry_wait):
    store, _, _, make, _ = env
    cam = BrokenSource({"A.MP4": b"a", "B.MP4": b"b", "C.MP4": b"c"}, {"A.MP4", "B.MP4"})
    with pytest.raises(SourceError):
        await make().run(cam, HERO)
    assert "C.MP4" not in cam.tries
    # A dropped link is not held against the second file.
    assert store.failed_runs(HERO.id, "100GOPRO/A.MP4", DAY, 1) == 1
    assert store.failed_runs(HERO.id, "100GOPRO/B.MP4", DAY, 1) == 0


async def test_thumbnails_saved_and_backfilled_without_failing_the_sync(env, tmp_path):
    store, library, _, make, logs = env
    thumbs = tmp_path / "thumbs"
    cam = FakeSource({"GX010001.MP4": b"a" * 10})
    await make().run(cam, HERO)  # Synced before thumbnails existed.
    assert not thumbs.exists()

    cam.files.update({"GX010002.MP4": b"b" * 10, "BROKEN.MP4": b"c" * 10})
    result = await make(thumbs=thumbs).run(cam, HERO)
    assert result.files == 2
    for name in ("GX010001.MP4", "GX010002.MP4"):
        rel = f"2025/2025-09-26/{name}"
        assert (thumbs / thumb_name(rel)).read_bytes() == f"jpeg:100GOPRO/{name}".encode()
    assert (library / "2025/2025-09-26/BROKEN.MP4").exists()
    assert any("no thumbnail for BROKEN.MP4" in msg for _, msg in logs)
