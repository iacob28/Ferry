import os
import subprocess
import threading

import pytest

from ferry.core.settings import Credential, DestinationConfig
from ferry.destinations import DestinationError, FtpDestination, SftpDestination

USER, PASSWORD = "ferry", "ferry-pass-123"


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    d = tmp_path_factory.mktemp("cert")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=nas",
                    "-keyout", d / "key.pem", "-out", d / "cert.pem"], check=True, capture_output=True)
    return d


def _ftp_server(root, mode, cert):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler, TLS_FTPHandler
    from pyftpdlib.servers import FTPServer

    if mode == "none":
        handler = type("Plain", (FTPHandler,), {})
    else:
        base = type("Tls", (TLS_FTPHandler,), {"tls_control_required": True, "tls_data_required": True})
        if mode == "implicit":
            def handle(self):
                self.secure_connection(self.ssl_context)

            def handle_ssl_established(self):
                TLS_FTPHandler.handle(self)

            base = type("Implicit", (base,), {"handle": handle, "handle_ssl_established": handle_ssl_established})
        handler = base
        handler.certfile, handler.keyfile = str(cert / "cert.pem"), str(cert / "key.pem")
    handler.auth_failed_timeout = 0.05
    auth = DummyAuthorizer()
    auth.add_user(USER, PASSWORD, str(root), perm="elradfmwMT")
    handler.authorizer = auth
    server = FTPServer(("127.0.0.1", 0), handler)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            server.serve_forever(timeout=0.05, blocking=False)
        server.close_all()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return server.address[1], lambda: (stop.set(), thread.join(5))


async def _conformance(dest, tmp_path, check_remote):
    await dest.test()
    big = tmp_path / "clip.mp4"
    big.write_bytes(os.urandom(3 << 20))
    assert await dest.stat("GoPro/2026/clip.mp4") is None
    sent = []
    remote = await dest.put(big, "GoPro/2026/clip.mp4", sent.append)
    assert remote.size == 3 << 20 and sum(sent) == 3 << 20
    small = tmp_path / "s.txt"
    small.write_bytes(b"second version")
    await dest.put(small, "GoPro/2026/clip.mp4", lambda n: None)  # replace in place
    assert (await dest.stat("GoPro/2026/clip.mp4")).size == len(b"second version")
    await dest.put(big, "a b/c+d.mp4", lambda n: None)
    got = b"".join([c async for c in dest.open_range("a b/c+d.mp4", 1000, (2 << 20) + 5)])
    assert got == big.read_bytes()[1000:(2 << 20) + 6]
    check_remote()
    dest.close()


@pytest.mark.parametrize("mode", ["none", "explicit", "implicit"])
async def test_ftp_destination(tmp_path, cert, mode):
    root = tmp_path / "server"
    root.mkdir()
    port, stop = _ftp_server(root, mode, cert)
    try:
        cfg = DestinationConfig(id="ftp", type="ftp", host="127.0.0.1", port=port, ftp_tls=mode, base_path="media/ferry")
        learned = {}
        dest = FtpDestination(cfg, Credential(id="c", username=USER, password=PASSWORD))
        dest.on_learn = learned.update
        assert dest.describe("x.mp4").startswith({"none": "ftp://", "explicit": "ftpes://", "implicit": "ftps://"}[mode])

        def check():
            assert (root / "media/ferry/GoPro/2026/clip.mp4").read_bytes() == b"second version"
            assert not list(root.rglob("*.partial"))

        await _conformance(dest, tmp_path, check)
        if mode == "none":
            assert learned == {}
        else:
            assert len(learned["fingerprint"]) == 64
            pinned = FtpDestination(cfg.model_copy(update={"fingerprint": "0" * 64}),
                                    Credential(id="c", username=USER, password=PASSWORD))
            with pytest.raises(DestinationError, match="different TLS certificate"):
                await pinned.test()
        with pytest.raises(DestinationError, match="login refused"):
            await FtpDestination(cfg, Credential(id="c", username=USER, password="wrong")).test()
    finally:
        stop()


SFTP = os.environ.get("FERRY_TEST_SFTP")  # host:port of `atmoz/sftp` with user ferry / ferry-pass-123


@pytest.mark.skipif(not SFTP, reason="set FERRY_TEST_SFTP=127.0.0.1:2222 (docker run atmoz/sftp ferry:ferry-pass-123:::upload)")
async def test_sftp_destination(tmp_path):
    host, port = SFTP.split(":")
    cfg = DestinationConfig(id="sftp", type="sftp", host=host, port=int(port), base_path=f"upload/ferry-{os.getpid()}-{id(tmp_path)}")
    learned = {}
    dest = SftpDestination(cfg, Credential(id="c", username=USER, password=PASSWORD))
    dest.on_learn = learned.update
    await _conformance(dest, tmp_path, lambda: None)
    assert learned["fingerprint"].startswith("SHA256:")
    again = SftpDestination(cfg.model_copy(update={"fingerprint": learned["fingerprint"]}),
                            Credential(id="c", username=USER, password=PASSWORD))
    assert (await again.stat("a b/c+d.mp4")).size == 3 << 20
    again.close()
    with pytest.raises(DestinationError, match="host key changed"):
        await SftpDestination(cfg.model_copy(update={"fingerprint": "SHA256:nope"}),
                              Credential(id="c", username=USER, password=PASSWORD)).test()
    with pytest.raises(DestinationError, match="login refused"):
        await SftpDestination(cfg, Credential(id="c", username=USER, password="wrong")).test()
    key_file = os.environ.get("FERRY_TEST_SFTP_KEY")
    if key_file:
        with open(key_file) as fh:
            keyed = SftpDestination(cfg, Credential(id="k", kind="ssh_key", username=USER, private_key=fh.read()))
        await keyed.test()
        keyed.close()


def test_sftp_needs_a_user():
    with pytest.raises(DestinationError, match="username"):
        SftpDestination(DestinationConfig(id="s", type="sftp", host="nas"), None)


def test_settings_merge_keeps_learned_pins():
    from ferry.core.settings import Settings
    from ferry.web import CLEAR_PIN, _merge

    current = Settings.model_validate({"version": 2, "destinations": [
        {"id": "nas", "type": "sftp", "host": "nas", "fingerprint": "SHA256:abc"}]})
    stale = {"version": 2, "destinations": [{"id": "nas", "type": "sftp", "host": "nas", "fingerprint": ""}]}
    assert _merge(current, stale).destinations[0].fingerprint == "SHA256:abc"
    moved = {"version": 2, "destinations": [{"id": "nas", "type": "sftp", "host": "other", "fingerprint": ""}]}
    assert _merge(current, moved).destinations[0].fingerprint == ""
    cleared = {"version": 2, "destinations": [{"id": "nas", "type": "sftp", "host": "nas", "fingerprint": CLEAR_PIN}]}
    assert _merge(current, cleared).destinations[0].fingerprint == ""
