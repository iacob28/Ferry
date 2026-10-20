"""Registered source providers."""

from __future__ import annotations

from .bambu import BambuProvider
from .base import Provider
from .gopro import GoProProvider
from .usbstorage import UsbStorageProvider

PROVIDERS: list[type[Provider]] = [GoProProvider, UsbStorageProvider, BambuProvider]
