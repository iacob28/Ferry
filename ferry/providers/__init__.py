"""Registered source providers."""

from __future__ import annotations

from .base import Provider
from .gopro import GoProProvider
from .usbstorage import UsbStorageProvider

PROVIDERS: list[type[Provider]] = [GoProProvider, UsbStorageProvider]
