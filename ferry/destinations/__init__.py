"""Destinations for offloaded media."""

from __future__ import annotations

from typing import Callable

from ..core.settings import Credential, DestinationConfig, Settings
from .base import Destination, DestinationError, RemoteObject
from .ftp import FtpDestination
from .gdrive import GoogleDriveDestination
from .local import LocalDestination
from .s3 import S3Destination
from .sftp import SftpDestination
from .smb import SmbDestination

__all__ = ["DESTINATION_TYPES", "Destination", "DestinationError", "FtpDestination", "GoogleDriveDestination",
           "LocalDestination", "RemoteObject", "S3Destination", "SftpDestination", "SmbDestination",
           "build_destination", "resolve_destination"]

DESTINATION_TYPES: dict[str, type[Destination]] = {
    "smb": SmbDestination, "sftp": SftpDestination, "ftp": FtpDestination, "local": LocalDestination,
    "s3": S3Destination, "gdrive": GoogleDriveDestination,
}


def build_destination(cfg: DestinationConfig, credential: Credential | None) -> Destination:
    cls = DESTINATION_TYPES.get(cfg.type)
    if cls is None:
        raise DestinationError(f"destination type {cfg.type!r} is not supported")
    if credential is not None and credential.kind not in cls.credential_kinds:
        raise DestinationError(f"{cfg.type} destinations cannot use {credential.kind} credentials")
    return cls(cfg, credential)


def resolve_destination(settings: Settings, dest_id: str | None, credentials: str | None = None,
                        on_learn: Callable[[str, dict], None] | None = None) -> Destination:
    """The destination with that id, logged in with `credentials` or else its own.

    on_learn(dest_id, fields) stores what it pins on first contact (SSH host key, TLS certificate).
    """
    cfg = settings.destination(dest_id)
    if cfg is None:
        raise DestinationError("no destination is configured" if not dest_id else f"unknown destination {dest_id!r}")
    cred_id = credentials or cfg.credentials
    credential = settings.credential(cred_id)
    if cred_id and credential is None:
        raise DestinationError(f"unknown credentials {cred_id!r}")
    destination = build_destination(cfg, credential)
    if on_learn is not None:
        destination.on_learn = lambda updates: on_learn(cfg.id, updates)
    return destination
