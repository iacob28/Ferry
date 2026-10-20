import asyncio
import os
import time

import pytest

from ferry.core.media import MediaItem, SourceError
from ferry.core.service import SyncService
from ferry.core.settings import Settings, SettingsStore
from ferry.core.store import Store
from ferry.providers.usbstorage import UsbStorageProvider, detect
from ferry.providers.usbstorage.profiles import identify, time_from_name
from ferry.providers.usbstorage.source import FolderSource, wall_clock
from ferry.thumbs import ThumbnailError, exif_thumbnail

NEO_CLIP = "DJI_20261020143012_0001_D.MP4"


def test_profiles_and_name_times():
    assert identify("2ca3", []).key == "dji"
    assert identify("", ["DJI_001"]).key == "dji"
    assert identify("", ["Camera01"]).key == "insta360"
    assert identify("", ["100GOPRO", "101GOPRO"]).key == "gopro"
    assert identify("", ["100CANON"]).key == "camera"
    assert time_from_name(NEO_CLIP) == 1792506612  # 2026-10-20 14:30:12 wall clock
    assert time_from_name("VID_20261020_143012_00_001.insv") == 1792506612
    assert time_from_name("PXL_20261020_143012123.mp4") == 1792506612
    assert time_from_name("DJI_0001.JPG") is None
    assert time_from_name("DJI_20261399999999_0001.MP4") is None
    assert wall_clock(1000.7, "vfat") == 1000


def test_exif_thumbnail():
    photo = b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00" + b"\xff\xd8tiny\xff\xd9" + b"\xff\xdbrest of the photo"
    assert exif_thumbnail(photo) == b"\xff\xd8tiny\xff\xd9"
    with pytest.raises(ThumbnailError):
        exif_thumbnail(b"\xff\xd8no preview here")


def make_neo(root):
    clips = root / "DCIM" / "DJI_001"
    clips.mkdir(parents=True)
    (clips / NEO_CLIP).write_bytes(b"v" * 3000)
    (clips / NEO_CLIP.replace(".MP4", ".LRF")).write_bytes(b"proxy")  # low-res proxy: not synced
    (clips / "DJI_0002.JPG").write_bytes(b"\xff\xd8\xff\xe1" + b"\xff\xd8thumb\xff\xd9" + b"x" * 100)
    os.utime(clips / "DJI_0002.JPG", (1792500000, 1792500000))
    (root / "DCIM" / ".Trashes").mkdir()
    return clips


async def test_folder_source_lists_copies_resumes_and_deletes(tmp_path):
    clips = make_neo(tmp_path / "neo")
    writable = []

    async def make_writable():
        writable.append(True)

    src = FolderSource(tmp_path / "neo", "vfat", make_writable=make_writable)
    items = {i.name: i for i in await src.list_media()}
    assert set(items) == {NEO_CLIP, "DJI_0002.JPG"}
    clip = items[NEO_CLIP]
    assert (clip.path, clip.created, clip.size) == (f"DCIM/DJI_001/{NEO_CLIP}", 1792506612, 3000)
    assert items["DJI_0002.JPG"].created == 1792500000

    staged = tmp_path / "staged.part"
    staged.write_bytes(b"v" * 1000)  # an interrupted earlier copy
    seen = []
    assert await src.download(clip, staged, seen.append) == 3000
    assert staged.read_bytes() == b"v" * 3000 and sum(seen) == 2000

    assert await src.thumbnail(items["DJI_0002.JPG"]) == b"\xff\xd8thumb\xff\xd9"
    await src.delete(items["DJI_0002.JPG"])
    await src.delete(clip)
    assert writable == [True]  # remounted read-write once
    assert sorted(p.name for p in clips.iterdir()) == [NEO_CLIP.replace(".MP4", ".LRF")]
    with pytest.raises(SourceError):
        await src.download(MediaItem("DCIM/gone.MP4", "gone.MP4", 0, 1), staged, seen.append)


async def test_block_candidates_from_sysfs(tmp_path, monkeypatch):
    usb = tmp_path / "devices/usb1/1-1"
    scsi = usb / "1-1:1.0/host0/target0:0:0/0:0:0:0"
    scsi.mkdir(parents=True)
    (usb / "idVendor").write_text("2ca3\n")
    (usb / "product").write_text("DJI Neo\n")
    (usb / "serial").write_text("1581F6Z\n")
    block = tmp_path / "block"
    for disk, target, size in (("sda", scsi, "4000"), ("sdb", tmp_path / "devices/pci0/ata1", "9000")):
        (block / disk / f"{disk}1").mkdir(parents=True)
        (block / disk / f"{disk}1" / "partition").write_text("1")
        (block / disk / "size").write_text(size)
        target.mkdir(parents=True, exist_ok=True)
        (block / disk / "device").symlink_to(target)
    probed = []

    async def fake_blkid(device):
        probed.append(device)
        return {"TYPE": "exfat", "UUID": "1A2B-3C4D", "LABEL": "NEO"}

    monkeypatch.setattr(detect, "SYS_BLOCK", block)
    monkeypatch.setattr(detect.sys, "platform", "linux")
    monkeypatch.setattr(detect, "_blkid", fake_blkid)
    (cand,) = await detect.block_candidates()
    assert probed == ["/dev/sda1"]  # sdb is not on USB
    assert (cand.id, cand.fstype, cand.vendor, cand.product, cand.label) == ("usb:1A2B-3C4D", "exfat", "2ca3", "DJI Neo", "NEO")


@pytest.fixture
def setup(tmp_path):
    drives = tmp_path / "media"
    make_neo(drives / "NEO")
    (drives / "BACKUP").mkdir()  # a drive without DCIM: ignored
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings.model_validate({
        "version": 2,
        "destinations": [{"id": "nas", "type": "local", "path": str(tmp_path / "nas")}],
        "defaults": {"destination": "nas"},
        "providers": {"usbstorage": {"options": {"watch_folders": [str(drives)]}}},
    }))
    service = SyncService(store, Store(tmp_path / "state.db"), tmp_path, providers=[UsbStorageProvider])
    return service, service.provider("usbstorage"), tmp_path / "nas"


async def _settle(service):
    for _ in range(100):
        if not service.sessions:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("sync did not finish")


async def test_unknown_drive_waits_for_adoption_then_syncs(setup):
    service, usb, nas = setup
    await usb._poll()
    (vol,) = usb.snapshot()["usb_volumes"]
    assert (vol["state"], vol["profile"], vol["model"], vol["device"]) == ("ready", "dji", "DJI", None)
    assert not service.sessions  # not adopted yet

    assert usb.request_sync("all", None) == 0  # "sync all" leaves unknown drives alone
    assert usb.request_sync("usb", vol["id"]) == 1  # syncing it by id adopts it
    await usb._poll()
    await _settle(service)
    assert (nas / f"DJI/2026/2026-10-20/{NEO_CLIP}").read_bytes() == b"v" * 3000
    assert (nas / "DJI/2026/2026-10-20/DJI_0002.JPG").exists()
    assert not list(nas.rglob("*.LRF"))
    (dev,) = service.settings.devices
    assert (dev.id, dev.provider, dev.name, dev.config["profile"]) == (vol["id"], "usbstorage", "DJI", "dji")
    assert service.store.recent_runs()[0]["status"] == "ok"
    assert usb.snapshot()["usb_volumes"][0]["state"] == "done"

    await usb._poll()  # already handled: no second run
    assert not service.sessions
    assert usb.request_sync("all", None) == 1  # adopted now
    await usb._poll()
    await _settle(service)
    assert [r["files"] for r in service.store.recent_runs()] == [0, 2]


async def test_adopted_drive_syncs_on_plug_in_with_device_rules(setup, tmp_path):
    service, usb, nas = setup
    await usb._poll()
    vid = usb.snapshot()["usb_volumes"][0]["id"]
    device = await usb.new_device("Neo", {"volume": vid})
    device.sync.path = "Drone/{device}"
    device.sync.delete_after_sync = True
    service.settings_store.update(lambda s: s.devices.append(device))
    await usb._poll()
    assert not service.sessions  # adopting alone does not sync
    drive, away = tmp_path / "media/NEO", tmp_path / "NEO-unplugged"
    drive.rename(away)
    await usb._poll()
    assert usb.snapshot()["usb_volumes"] == []
    away.rename(drive)  # plugged back in
    await usb._poll()
    await _settle(service)
    assert (nas / f"Drone/Neo/2026/2026-10-20/{NEO_CLIP}").exists()
    remaining = sorted(p.name for p in (tmp_path / "media/NEO/DCIM/DJI_001").iterdir())
    assert remaining == [NEO_CLIP.replace(".MP4", ".LRF")]  # verified files deleted, proxy kept
    with pytest.raises(ValueError, match="not connected"):
        await usb.new_device("x", {"volume": "usb:nope"})


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg not installed")
async def test_video_thumbnail_with_ffmpeg(tmp_path):
    import subprocess

    from ferry.thumbs import video_thumbnail

    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:duration=0.5",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    jpeg = await video_thumbnail(clip)  # shorter than the 1 s seek: falls back to the first frame
    assert jpeg.startswith(b"\xff\xd8")


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg not installed")
async def test_photo_without_exif_preview_gets_an_ffmpeg_thumbnail(tmp_path):
    import subprocess

    clips = tmp_path / "cam/DCIM/100MEDIA"
    clips.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:duration=1",
                    "-frames:v", "1", str(clips / "DJI_0001.JPG")], check=True, stdin=subprocess.DEVNULL)
    src = FolderSource(tmp_path / "cam")
    (item,) = await src.list_media()
    assert (await src.thumbnail(item)).startswith(b"\xff\xd8")


# --- SD cards in a card reader ------------------------------------------------------------


def _card_service(tmp_path, settings_extra):
    from ferry.providers.bambu import BambuProvider
    from ferry.providers.gopro import GoProProvider

    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings.model_validate({
        "version": 2,
        "destinations": [{"id": "nas", "type": "local", "path": str(tmp_path / "nas")}],
        "defaults": {"destination": "nas"},
        "providers": {"usbstorage": {"options": {"watch_folders": [str(tmp_path / "media")]}}},
        **settings_extra,
    }))
    service = SyncService(store, Store(tmp_path / "state.db"), tmp_path,
                          providers=[UsbStorageProvider, GoProProvider, BambuProvider])
    return service, service.provider("usbstorage")


async def test_gopro_card_syncs_as_its_camera_without_duplicates(tmp_path):
    card = tmp_path / "media/GOPRO"
    (card / "DCIM/100GOPRO").mkdir(parents=True)
    (card / "MISC").mkdir()
    (card / "MISC/version.txt").write_text('{\n"info version":"2.0",\n"firmware version":"HD9.01.01.72.00",\n'
                                           '"camera type":"HERO9 Black",\n"camera serial number":"C3441325050522"\n}\n')
    clip = card / "DCIM/100GOPRO/GX010474.MP4"
    clip.write_bytes(b"g" * 2000)
    os.utime(clip, (1792506612, 1792506612))
    service, usb = _card_service(tmp_path, {
        "providers": {"usbstorage": {"options": {"watch_folders": [str(tmp_path / "media")]}},
                      "gopro": {"sync": {"path": "GoPro"}}},
        "devices": [{"id": "gopro:AA", "provider": "gopro", "name": "Helmet",
                     "config": {"address": "AA", "serial": "C3441325050522"}}],
    })
    nas = tmp_path / "nas/GoPro/2026/2026-10-20"
    nas.mkdir(parents=True)
    (nas / "GX010474.MP4").write_bytes(b"g" * 2000)  # synced earlier from the camera itself
    await usb._poll()
    (vol,) = usb.snapshot()["usb_volumes"]
    assert (vol["profile"], vol["rules"], vol["device"], vol["adopt"], vol["model"]) == \
        ("gopro", "gopro", "gopro:AA", False, "HERO9 Black")
    await _settle(service)
    assert sorted(p.name for p in nas.iterdir()) == ["GX010474.MP4"]  # not uploaded twice, no _suffix copy
    # Same keys as the camera's own media list (serial, path relative to DCIM).
    (item,), _ = service.store.library()
    assert (item["camera"], item["path"], item["provider"], item["dest"]) == \
        ("C3441325050522", "100GOPRO/GX010474.MP4", "gopro", "GoPro/2026/2026-10-20/GX010474.MP4")
    run = service.store.recent_runs()[0]
    assert (run["status"], run["camera"]) == ("ok", "Helmet")


async def test_gopro_card_follows_the_gopro_auto_switch(tmp_path):
    card = tmp_path / "media/GOPRO"
    (card / "DCIM/100GOPRO").mkdir(parents=True)
    (card / "DCIM/100GOPRO/GX010001.MP4").write_bytes(b"x")
    service, usb = _card_service(tmp_path, {"providers": {
        "usbstorage": {"options": {"watch_folders": [str(tmp_path / "media")]}}, "gopro": {"enabled": False}}})
    await usb._poll()
    (vol,) = usb.snapshot()["usb_volumes"]
    # No MISC/version.txt: an anonymous GoPro card is adopted like any drive.
    assert (vol["profile"], vol["adopt"]) == ("gopro", True)


async def test_bambu_card_syncs_as_the_printer(tmp_path):
    card = tmp_path / "media/P1S"
    (card / "timelapse/thumbnail").mkdir(parents=True)
    (card / "timelapse/video_2026-10-20_14-30-12.mp4").write_bytes(b"t" * 500)
    (card / "timelapse/video_2026-10-20_14-30-12.jpg").write_bytes(b"not a video")
    (card / "Benchy.gcode.3mf").write_bytes(b"model")
    service, usb = _card_service(tmp_path, {
        "providers": {"usbstorage": {"options": {"watch_folders": [str(tmp_path / "media")]}},
                      "bambu": {"sync": {"path": "3D/{device}"}}},
        "devices": [{"id": "bambu:01P00A1", "provider": "bambu", "name": "Workshop",
                     "config": {"host": "10.0.0.5", "serial": "01P00A1", "access_code": "x", "model": "P1S"}}],
    })
    usb.service.providers["bambu"]._tick = lambda: None
    await usb._poll()
    (vol,) = usb.snapshot()["usb_volumes"]
    assert (vol["profile"], vol["rules"], vol["device"], vol["adopt"]) == ("bambu", "bambu", "bambu:01P00A1", False)
    await _settle(service)
    assert (tmp_path / "nas/3D/Workshop/2026/2026-10-20/video_2026-10-20_14-30-12.mp4").read_bytes() == b"t" * 500
    # The key matches what the printer's FTPS listing produces, so the printer won't send it again.
    assert service.store.is_synced("bambu:01P00A1", "timelapse/video_2026-10-20_14-30-12.mp4", 1792506612, 500)


async def test_bambu_card_without_a_known_printer_waits_for_adoption(tmp_path):
    (tmp_path / "media/P1S/timelapse").mkdir(parents=True)
    service, usb = _card_service(tmp_path, {})
    await usb._poll()
    (vol,) = usb.snapshot()["usb_volumes"]
    assert (vol["profile"], vol["rules"], vol["adopt"]) == ("bambu", "bambu", True)
    assert not service.sessions


async def test_builtin_sd_slot_is_opt_in(tmp_path, monkeypatch):
    block = tmp_path / "block"
    (block / "mmcblk0/mmcblk0p1").mkdir(parents=True)
    (block / "mmcblk0/mmcblk0p1/partition").write_text("1")
    (block / "mmcblk0/size").write_text("1000")
    (tmp_path / "mmc").mkdir()
    (block / "mmcblk0/device").symlink_to(tmp_path / "mmc")

    async def fake_blkid(device):
        return {"TYPE": "vfat", "UUID": "5A5A-0001", "LABEL": "GOPRO"}

    monkeypatch.setattr(detect, "SYS_BLOCK", block)
    monkeypatch.setattr(detect.sys, "platform", "linux")
    monkeypatch.setattr(detect, "_blkid", fake_blkid)
    assert await detect.block_candidates() == []
    (card,) = await detect.block_candidates(sd_slot=True)
    assert (card.id, card.source, card.product) == ("usb:5A5A-0001", "/dev/mmcblk0p1", "SD card")
