import asyncio
import subprocess
import threading
import time

import pytest

from ferry.core.media import MediaItem
from ferry.core.service import SyncService
from ferry.core.settings import Settings, SettingsStore
from ferry.core.store import Store
from ferry.providers.bambu import BambuProvider
from ferry.providers.bambu import provider as bambu_provider
from ferry.providers.bambu.discovery import parse_notify
from ferry.providers.bambu.ftps import FtpsError, PrinterFiles, parse_list
from ferry.providers.bambu.mqtt import PrinterState
from ferry.providers.bambu.source import PrinterSource

CODE = "12345678"
CLIP = "video_2026-10-20_14-30-12.mp4"


def test_parse_list_and_notify():
    files = parse_list([
        "-rw-rw-rw-   1 root  root   1234567 Oct 20 14:30 video_2026-10-20_14-30-12.mp4",
        "drwxrwxrwx   1 root  root         0 Oct 20 14:30 thumbnail",
        "garbage",
    ])
    assert [(f.name, f.size, f.is_dir) for f in files] == [(CLIP, 1234567, False), ("thumbnail", 0, True)]
    notify = (b"NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nServer: Buildroot/2018.02-rc3 UPnP/1.0 ssdpd/1.8\r\n"
              b"Location: 192.168.1.50\r\nNT: urn:bambulab-com:device:3dprinter:1\r\nUSN: 01P00A123456789\r\n"
              b"DevModel.bambu.com: C12\r\nDevName.bambu.com: Workshop P1S\r\nDevConnect.bambu.com: cloud\r\n\r\n")
    assert parse_notify(notify, "192.168.1.50") == {"host": "192.168.1.50", "serial": "01P00A123456789",
                                                     "name": "Workshop P1S", "model": "P1S", "connect": "cloud"}
    assert parse_notify(b"NOTIFY * HTTP/1.1\r\nUSN: x\r\n", "1.2.3.4") is None


def test_printer_state_detects_the_end_of_a_print():
    s = PrinterState()
    s.apply({"print": {"gcode_state": "RUNNING", "mc_percent": 40, "subtask_name": "benchy"}})
    assert s.printing and s.percent == 40 and s.ended_at is None
    s.apply({"print": {"mc_percent": 99}})  # partial update keeps the state
    assert s.gcode_state == "RUNNING" and s.percent == 99
    s.apply({"print": {"gcode_state": "FINISH", "mc_percent": 100}})
    assert not s.printing and s.ended_at is not None
    s2 = PrinterState()
    s2.apply({"print": {"gcode_state": "FINISH"}})  # already finished when we connected
    assert s2.ended_at is None
    s2.apply({"info": {}})


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    d = tmp_path_factory.mktemp("cert")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=printer",
                    "-keyout", d / "key.pem", "-out", d / "cert.pem"], check=True, capture_output=True)
    return d


@pytest.fixture
def printer(tmp_path, cert):
    """A local implicit-FTPS server laid out like a P1S SD card."""
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import TLS_FTPHandler
    from pyftpdlib.servers import FTPServer

    card = tmp_path / "card"
    (card / "timelapse/thumbnail").mkdir(parents=True)
    (card / "timelapse" / CLIP).write_bytes(bytes(range(256)) * 100)
    (card / "timelapse/thumbnail/video_2026-10-20_14-30-12.jpg").write_bytes(b"\xff\xd8thumb\xff\xd9")
    (card / "timelapse/video_2026-10-21_09-00-00.mp4").write_bytes(b"")  # still being written
    (card / "model.3mf").write_bytes(b"not media")

    class Implicit(TLS_FTPHandler):
        def handle(self):
            self.secure_connection(self.ssl_context)

        def handle_ssl_established(self):
            TLS_FTPHandler.handle(self)

    Implicit.certfile = str(cert / "cert.pem")
    Implicit.auth_failed_timeout = 0.05
    Implicit.keyfile = str(cert / "key.pem")
    auth = DummyAuthorizer()
    auth.add_user("bblp", CODE, str(card), perm="elradfmw")
    Implicit.authorizer = auth
    server = FTPServer(("127.0.0.1", 0), Implicit)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            server.serve_forever(timeout=0.05, blocking=False)
        server.close_all()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield server.address[1], card
    stop.set()
    thread.join(5)


def test_ftps_login_list_resume_and_pin(printer, tmp_path):
    port, card = printer
    files = PrinterFiles("127.0.0.1", CODE, port=port).open()
    pin = files.fingerprint
    assert len(pin) == 64
    assert CLIP in [f.name for f in files.listdir("/timelapse")]
    assert files.listdir("/missing") == []
    dest = tmp_path / "part"
    dest.write_bytes((bytes(range(256)) * 100)[:1000])
    got = []
    files.download(f"/timelapse/{CLIP}", dest, 1000, got.append)
    assert dest.read_bytes() == bytes(range(256)) * 100 and sum(got) == 24600
    files.close()

    with pytest.raises(FtpsError, match="access code"):
        PrinterFiles("127.0.0.1", "wrong", port=port).open()
    with pytest.raises(FtpsError, match="different TLS certificate"):
        PrinterFiles("127.0.0.1", CODE, fingerprint="0" * 64, port=port).open()
    PrinterFiles("127.0.0.1", CODE, fingerprint=pin, port=port).open().close()


async def test_printer_source(printer, tmp_path):
    port, card = printer
    files = await asyncio.to_thread(PrinterFiles("127.0.0.1", CODE, port=port).open)
    source = PrinterSource(files, ["timelapse"], ["mp4", "avi"])
    (item,) = await source.list_media()
    assert item == MediaItem(f"timelapse/{CLIP}", CLIP, 1792506612, 25600)
    assert await source.thumbnail(item) == b"\xff\xd8thumb\xff\xd9"
    dest = tmp_path / "staged"
    await asyncio.gather(source.download(item, dest, lambda n: None), source.thumbnail(item))  # serialised
    assert dest.stat().st_size == 25600
    await source.delete(item)
    assert not (card / "timelapse" / CLIP).exists()
    await asyncio.to_thread(files.close)


class FakeMonitor:
    instances = []

    def __init__(self, host, serial, code, on_change):
        self.state = PrinterState()
        FakeMonitor.instances.append(self)

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture
def bambu(tmp_path, monkeypatch, printer):
    monkeypatch.setattr(bambu_provider, "PrinterMonitor", FakeMonitor)
    FakeMonitor.instances = []
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings.model_validate({
        "version": 2,
        "destinations": [{"id": "nas", "type": "local", "path": str(tmp_path / "nas")}],
        "defaults": {"destination": "nas"},
        "providers": {"bambu": {"sync": {"path": "3D/{device}", "folder_template": "{year}-{month}"},
                                "options": {"poll_minutes": 0, "delay_after_print_seconds": 0}}},
    }))
    service = SyncService(store, Store(tmp_path / "state.db"), tmp_path, providers=[BambuProvider])
    return service, service.provider("bambu"), printer[0], tmp_path / "nas"


async def _settle(service):
    for _ in range(200):
        if not service.sessions:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("sync did not finish")


async def test_add_printer_and_sync_after_a_print(bambu):
    service, provider, port, nas = bambu
    with pytest.raises(ValueError, match="access code"):
        await provider.new_device("", {"host": "127.0.0.1", "serial": "01p00a1", "access_code": "nope", "port": port})
    device = await provider.new_device("Workshop", {"host": "127.0.0.1", "serial": "01p00a1", "access_code": CODE,
                                                    "port": port, "model": "P1S"})
    assert (device.id, device.config["model"], len(device.config["cert_sha256"])) == ("bambu:01P00A1", "P1S", 64)
    service.settings_store.update(lambda s: s.devices.append(device))

    provider._tick()  # poll_minutes = 0: nothing happens until a print ends
    (monitor,) = FakeMonitor.instances
    assert not service.sessions
    monitor.state.apply({"print": {"gcode_state": "RUNNING"}})
    provider.request_sync("all", None)
    provider._tick()
    assert not service.sessions  # not even a manual sync while printing
    monitor.state.apply({"print": {"gcode_state": "FINISH"}})
    provider._tick()
    assert list(service.sessions) == ["bambu:bambu:01P00A1"]
    await _settle(service)
    assert (nas / f"3D/Workshop/2026-10/{CLIP}").stat().st_size == 25600
    run = service.store.recent_runs()[0]
    assert (run["status"], run["mode"], run["provider"], run["camera"]) == ("ok", "lan", "bambu", "Workshop")
    assert (service.thumbs).exists()
    assert provider.snapshot()["printers"]["bambu:01P00A1"]["last_error"] == ""


async def test_mqtt_monitor_callbacks_reach_the_loop():
    from types import SimpleNamespace

    from ferry.providers.bambu.mqtt import PrinterMonitor

    changes = []
    monitor = PrinterMonitor("printer", "01P00A1", CODE, on_change=lambda: changes.append(1))
    sent = []
    client = SimpleNamespace(subscribe=lambda topic: sent.append(("sub", topic)),
                             publish=lambda topic, payload: sent.append(("pub", topic, payload)))
    ok = SimpleNamespace(is_failure=False)
    await asyncio.to_thread(monitor._connected, client, None, None, ok)  # paho calls from its own thread
    await asyncio.sleep(0)
    assert monitor.state.connected
    assert sent[0] == ("sub", "device/01P00A1/report") and '"pushall"' in sent[1][2]
    msg = SimpleNamespace(payload=b'{"print": {"gcode_state": "RUNNING", "mc_percent": 12}}')
    await asyncio.to_thread(monitor._message, client, None, msg)
    await asyncio.sleep(0)
    assert (monitor.state.gcode_state, monitor.state.percent) == ("RUNNING", 12)
    await asyncio.to_thread(monitor._connected, client, None, None, SimpleNamespace(is_failure=True, __str__=None))
    await asyncio.sleep(0)
    assert not monitor.state.connected and monitor.state.error.startswith("MQTT refused")
    assert len(changes) == 3
