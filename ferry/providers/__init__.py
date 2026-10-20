"""Registered source providers."""

from __future__ import annotations

from .base import Provider
from .gopro import GoProProvider

PROVIDERS: list[type[Provider]] = [GoProProvider]
