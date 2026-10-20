"""Destinations for offloaded media."""

from __future__ import annotations

from ..core.settings import Settings
from .base import Destination, DestinationError, RemoteObject
from .local import LocalDestination
from .smb import SmbDestination

__all__ = ["Destination", "DestinationError", "LocalDestination", "RemoteObject", "SmbDestination",
           "make_destination"]


def make_destination(settings: Settings) -> Destination:
    if settings.target == "local":
        return LocalDestination(settings.local_path)
    return SmbDestination(settings.smb)
