"""Loader for config/scan_venues.toml (written from the venue research, never from guesses)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class VenueCfg:
    name: str
    raw: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, key: str) -> Any:
        try:
            return self.raw[key]
        except KeyError:
            raise AttributeError(key) from None

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enabled", False))

    @property
    def skip_reason(self) -> str:
        return str(self.raw.get("skip_reason", "disabled"))


@dataclass(frozen=True)
class ScanConfig:
    hosts: frozenset[str]
    venue: dict[str, VenueCfg]
    forecastex: dict[str, Any]


def load(path: Path = Path("config/scan_venues.toml")) -> ScanConfig:
    d = tomllib.loads(path.read_text())
    return ScanConfig(
        hosts=frozenset(d.get("hosts", {}).get("allow", [])),
        venue={k: VenueCfg(k, v) for k, v in d.get("venue", {}).items()},
        forecastex=dict(d.get("forecastex", {})),
    )
