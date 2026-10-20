from pathlib import Path

import pytest

from ferry.core.engine import SyncEngine, thumb_name
from ferry.core.media import MediaItem, SourceError
from ferry.core.settings import Settings
from ferry.core.store import Store
from ferry.destinations import LocalDestination

DAY = 1758902400  # 2025-09-26


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
    settings = Settings(target="local", local_path=str(library))
    logs = []

    def make(settings=settings, thumbs=None):
        return SyncEngine(store, settings, LocalDestination(settings.local_path), tmp_path / "staging",
                          lambda level, msg: logs.append((level, msg)), lambda p: None, thumbs)

    (tmp_path / "staging").mkdir()
    return store, library, settings, make, logs


async def test_offloads_new_files_and_skips_known(env):
    store, library, _, make, _ = env
    cam = FakeSource({"GX010001.MP4": b"a" * 1000, "GX010002.MP4": b"b" * 500})
    result = await make().run(cam, "C344", "HERO9 Black")
    assert (result.files, result.bytes) == (2, 1500)
    assert (library / "2025/2025-09-26/GX010001.MP4").read_bytes() == b"a" * 1000
    assert not list((library.parent / "staging").rglob("*.part"))
    # Second run: nothing new.
    assert (await make().run(cam, "C344", "HERO9 Black")).files == 0
    assert store.totals() == {"files": 2, "bytes": 1500}


async def test_resumes_after_dropped_link(env):
    _, library, _, make, logs = env
    cam = FakeSource({"GX010003.MP4": bytes(range(256)) * 40}, fail_once={"GX010003.MP4"})
    result = await make().run(cam, "C344", "HERO9 Black")
    assert result.files == 1
    assert (library / "2025/2025-09-26/GX010003.MP4").read_bytes() == bytes(range(256)) * 40
    assert any("retrying" in msg for _, msg in logs)


async def test_name_clash_with_different_file_gets_suffix(env):
    _, library, _, make, _ = env
    clash = library / "2025/2025-09-26/GX010001.MP4"
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"older file from before a card format")
    await make().run(FakeSource({"GX010001.MP4": b"new"}), "C344", "HERO9 Black")
    assert clash.read_bytes().startswith(b"older")
    assert (library / f"2025/2025-09-26/GX010001_{DAY}.MP4").read_bytes() == b"new"


async def test_existing_identical_file_is_not_reuploaded(env):
    _, library, _, make, _ = env
    dest = library / "2025/2025-09-26/GX010001.MP4"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"same")
    result = await make().run(FakeSource({"GX010001.MP4": b"same"}), "C344", "HERO9 Black")
    assert result.skipped_existing == 1


async def test_delete_only_when_enabled(env):
    _, _, settings, make, _ = env
    cam = FakeSource({"GX010001.MP4": b"x"})
    await make().run(cam, "C344", "HERO9 Black")
    assert cam.deleted == []

    deleting = settings.model_copy(update={"delete_after_sync": True})
    cam2 = FakeSource({"GX010009.MP4": b"y"})
    result = await make(deleting).run(cam2, "C344", "HERO9 Black")
    assert cam2.deleted == ["100GOPRO/GX010009.MP4"]
    assert result.deleted == 1


async def test_thumbnails_saved_and_backfilled_without_failing_the_sync(env, tmp_path):
    store, library, _, make, logs = env
    thumbs = tmp_path / "thumbs"
    cam = FakeSource({"GX010001.MP4": b"a" * 10})
    await make().run(cam, "C344", "HERO9 Black")  # Synced before thumbnails existed.
    assert not thumbs.exists()

    cam.files.update({"GX010002.MP4": b"b" * 10, "BROKEN.MP4": b"c" * 10})
    result = await make(thumbs=thumbs).run(cam, "C344", "HERO9 Black")
    assert result.files == 2
    for name in ("GX010001.MP4", "GX010002.MP4"):
        rel = f"2025/2025-09-26/{name}"
        assert (thumbs / thumb_name(rel)).read_bytes() == f"jpeg:100GOPRO/{name}".encode()
    assert (library / "2025/2025-09-26/BROKEN.MP4").exists()
    assert any("no thumbnail for BROKEN.MP4" in msg for _, msg in logs)
