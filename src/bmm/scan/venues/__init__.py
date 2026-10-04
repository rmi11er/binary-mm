"""Venue adapters: one module per venue, one shared VenueAdapter protocol (models.py)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bmm.scan.config import VenueCfg
from bmm.scan.http import GetOnlyClient
from bmm.scan.venues.gemini import Gemini
from bmm.scan.venues.kalshi import Kalshi
from bmm.scan.venues.pm_us import PmUs

ADAPTERS: dict[str, Callable[[GetOnlyClient, VenueCfg], Any]] = {
    "pm_us": PmUs,
    "kalshi": Kalshi,
    "gemini": Gemini,
}
