from ferry.core.engine import DeviceInfo, dest_rel
from ferry.core.media import MediaItem
from ferry.core.settings import Credential, Device, Settings
from ferry.providers.gopro.ble import PacketAssembler, frame, parse_powered_on
from ferry.providers.gopro.camera import parse_media_list
from ferry.web import MASK, _merge, _public


def test_frame_short_message():
    assert frame(bytes([0x17, 0x01, 0x01])) == bytes([0x03, 0x17, 0x01, 0x01])


def test_assembler_single_packet():
    assert PacketAssembler().feed(bytes([0x02, 0x17, 0x00])) == bytes([0x17, 0x00])


def test_assembler_fragmented_13bit():
    payload = bytes(range(25))
    asm = PacketAssembler()
    first = bytes([0x20 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:18]
    assert asm.feed(first) is None
    assert asm.feed(bytes([0x80]) + payload[18:]) == payload


def test_assembler_ignores_orphan_continuation():
    assert PacketAssembler().feed(bytes([0x80, 0x01])) is None


def test_powered_on_flag():
    assert parse_powered_on({0x02F2: bytes([0x02, 0x01, 0x00])}) is True
    assert parse_powered_on({0x02F2: bytes([0x02, 0x00, 0x00])}) is False
    assert parse_powered_on({}) is None


def test_parse_media_list_with_group():
    data = {"media": [{"d": "100GOPRO", "fs": [
        {"n": "GX010474.MP4", "cre": "1757840000", "s": "4000000000"},
        {"n": "G0010012.JPG", "cre": "1757840100", "b": "12", "l": "15", "m": ["13"]},
    ]}]}
    files = parse_media_list(data)
    assert files[0] == MediaItem("100GOPRO/GX010474.MP4", "GX010474.MP4", 1757840000, 4000000000)
    assert [f.path for f in files[1:]] == ["100GOPRO/G0010012.JPG", "100GOPRO/G0010014.JPG", "100GOPRO/G0010015.JPG"]
    assert all(f.size is None for f in files[1:])


def _policy(**defaults):
    return Settings(defaults=defaults).effective("gopro")


def test_dest_rel_uses_camera_wall_clock():
    m = MediaItem("100GOPRO/GX010474.MP4", "GX010474.MP4", 1758902400, 1)  # 2025-09-26 16:00 camera time
    hero = DeviceInfo("gopro", "GoPro", "C344", "HERO9 Black", "Helmet cam")
    assert dest_rel(_policy(path=""), hero, m) == "2025/2025-09-26/GX010474.MP4"
    assert dest_rel(_policy(), hero, m) == "GoPro/2025/2025-09-26/GX010474.MP4"
    custom = _policy(path="Media/{provider}", folder_template="{camera}/{year}-{month}/{device}")
    assert dest_rel(custom, hero, m) == "Media/GoPro/HERO9 Black/2025-09/Helmet cam/GX010474.MP4"
    assert dest_rel(_policy(path="", folder_template=""), hero, m) == "GX010474.MP4"
    slashy = DeviceInfo("gopro", "GoPro", "C344", "HERO9", "a/../b")
    assert dest_rel(_policy(path="{device}", folder_template=""), slashy, m) == "a-..-b/GX010474.MP4"


def test_settings_secret_masking_and_merge():
    current = Settings(credentials=[Credential(id="nas", username="u", password="secret")],
                       devices=[Device(id="gopro:AA", provider="gopro", config={"address": "AA"})])
    public = _public(current)
    assert public["credentials"][0]["password"] == MASK
    public["credentials"][0]["username"] = "other"
    public["devices"] = []
    merged = _merge(current, public)
    assert merged.credentials[0].password == "secret"
    assert merged.credentials[0].username == "other"
    assert [d.id for d in merged.devices] == ["gopro:AA"]


async def test_camera_requests_with_a_path_query_parameter():
    import httpx

    from ferry.providers.gopro.camera import GoProHttp

    seen = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, content=b"\xff\xd8jpeg")

    cam = GoProHttp("172.20.100.51")
    cam._client = httpx.AsyncClient(base_url="http://cam", transport=httpx.MockTransport(handler))
    item = MediaItem("100GOPRO/GX010001.MP4", "GX010001.MP4", 0, 1)
    assert await cam.thumbnail(item) == b"\xff\xd8jpeg"
    await cam.delete(item)
    await cam.close()
    assert seen == ["/gopro/media/thumbnail?path=100GOPRO/GX010001.MP4",
                    "/gopro/media/delete/file?path=100GOPRO/GX010001.MP4"]
