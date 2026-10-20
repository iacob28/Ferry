"""FTP over TLS clients shared by the Bambu provider and the FTP destination.

NAS boxes and printers present self-signed certificates, so instead of CA verification the
certificate is pinned: the first connection records its SHA-256 fingerprint and later ones must
present the same one. Data connections resume the control channel's TLS session, which several
servers (vsftpd's require_ssl_reuse, Bambu printers) insist on.
"""

from __future__ import annotations

import ftplib
import hashlib
import socket
import ssl

TIMEOUT = 30


class CertificateChanged(Exception):
    pass


def _context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # pinned instead, see the module docstring
    return context


class ExplicitFtps(ftplib.FTP_TLS):
    """FTPES: plain connect, then AUTH TLS."""

    def __init__(self, fingerprint: str | None = None, timeout: float = TIMEOUT):
        super().__init__(context=_context(), timeout=timeout)
        self.expected_fingerprint = fingerprint or None
        self.fingerprint: str | None = None

    def _pin(self) -> None:
        cert = self.sock.getpeercert(binary_form=True) or b""
        self.fingerprint = hashlib.sha256(cert).hexdigest()
        if self.expected_fingerprint and self.fingerprint != self.expected_fingerprint:
            self.sock.close()
            raise CertificateChanged("the server presented a different TLS certificate than before; if it was "
                                     "reset or replaced, clear the pinned certificate and connect again")

    def auth(self):
        resp = super().auth()
        self._pin()
        return resp

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


class ImplicitFtps(ExplicitFtps):
    """FTPS on port 990: TLS from the first byte (ftplib only does AUTH TLS)."""

    def connect(self, host: str = "", port: int = 990, timeout: float = TIMEOUT, source_address=None) -> str:
        self.host, self.port, self.timeout = host, port, timeout
        raw = socket.create_connection((host, port), timeout)
        self.sock = self.context.wrap_socket(raw, server_hostname=host)
        self._pin()
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def login(self, user="", passwd="", acct="", secure=True):
        # Already encrypted: skip FTP_TLS.login's AUTH TLS.
        return ftplib.FTP.login(self, user, passwd, acct)
