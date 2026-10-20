"""Destinations for offloaded media."""

from __future__ import annotations

from ..core.settings import Credential, DestinationConfig, Settings
from .base import Destination, DestinationError, RemoteObject
from .local import LocalDestination
from .smb import SmbDestination

__all__ = ["DESTINATION_TYPES", "Destination", "DestinationError", "LocalDestination", "RemoteObject",
           "SmbDestination", "build_destination", "resolve_destination"]

DESTINATION_TYPES: dict[str, type[Destination]] = {"smb": SmbDestination, "local": LocalDestination}


def build_destination(cfg: DestinationConfig, credential: Credential | None) -> Destination:
    cls = DESTINATION_TYPES.get(cfg.type)
    if cls is None:
        raise DestinationError(f"destination type {cfg.type!r} is not supported")
    if credential is not None and credential.kind not in cls.credential_kinds:
        raise DestinationError(f"{cfg.type} destinations cannot use {credential.kind} credentials")
    return cls(cfg, credential)


def resolve_destination(settings: Settings, dest_id: str | None, credentials: str | None = None) -> Destination:
    """The destination with that id, logged in with `credentials` or else its own."""
    cfg = settings.destination(dest_id)
    if cfg is None:
        raise DestinationError("no destination is configured" if not dest_id else f"unknown destination {dest_id!r}")
    cred_id = credentials or cfg.credentials
    credential = settings.credential(cred_id)
    if cred_id and credential is None:
        raise DestinationError(f"unknown credentials {cred_id!r}")
    return build_destination(cfg, credential)
