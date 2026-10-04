"""Product specs, reference-future month selection and option-expiry matching."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, fields, replace
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

Venue = Literal["forecastex", "cme"]
RefRule = Literal["lead_quarterly", "named_gc", "under_con_id"]
Role = Literal["match", "below", "above"]


@dataclass(frozen=True)
class ProductSpec:
    key: str
    venue: Venue
    ib_symbol: str  # mapping to be confirmed on first connect
    ib_sec_type: str
    ib_exchange: str
    trading_class: str | None
    fut_symbol: str
    fut_exchange: str
    opt_exchange: str
    fix_ct: tuple[int, int]
    payout_scale: float
    ref_rule: RefRule


PRODUCTS: dict[str, ProductSpec] = {
    "forecastex-spx": ProductSpec(
        key="forecastex-spx", venue="forecastex", ib_symbol="FES", ib_sec_type="OPT",
        ib_exchange="FORECASTX", trading_class=None, fut_symbol="ES", fut_exchange="CME",
        opt_exchange="CME", fix_ct=(15, 0), payout_scale=1.0, ref_rule="lead_quarterly",
    ),
    "forecastex-ndx": ProductSpec(
        key="forecastex-ndx", venue="forecastex", ib_symbol="FNQ", ib_sec_type="OPT",
        ib_exchange="FORECASTX", trading_class=None, fut_symbol="NQ", fut_exchange="CME",
        opt_exchange="CME", fix_ct=(15, 0), payout_scale=1.0, ref_rule="lead_quarterly",
    ),
    "forecastex-gold": ProductSpec(
        key="forecastex-gold", venue="forecastex", ib_symbol="METLG", ib_sec_type="OPT",
        ib_exchange="FORECASTX", trading_class=None, fut_symbol="GC", fut_exchange="COMEX",
        opt_exchange="COMEX", fix_ct=(12, 30), payout_scale=1.0, ref_rule="named_gc",
    ),
    "cme-gold": ProductSpec(
        key="cme-gold", venue="cme", ib_symbol="GC", ib_sec_type="FOP",
        ib_exchange="COMEX", trading_class="ECGC", fut_symbol="GC", fut_exchange="COMEX",
        opt_exchange="COMEX", fix_ct=(12, 30), payout_scale=1.0, ref_rule="under_con_id",
    ),
}  # fmt: skip


def load_products(path: Path | None) -> dict[str, ProductSpec]:
    """Defaults, with per-product field overrides from `[products.<key>]` TOML tables."""
    out = dict(PRODUCTS)
    if path is None:
        return out
    data = tomllib.loads(Path(path).read_text())
    valid = {f.name for f in fields(ProductSpec)} - {"key"}
    for key, over in data.get("products", {}).items():
        if key not in out:
            raise ValueError(f"unknown product {key!r}")
        bad = set(over) - valid
        if bad:
            raise ValueError(f"unknown fields for {key}: {sorted(bad)}")
        over = dict(over)
        if "fix_ct" in over:
            h, m = over["fix_ct"]
            over["fix_ct"] = (int(h), int(m))
        out[key] = replace(out[key], **over)
    return out


def lead_month(question_date: date, final_settlement_dates: list[date]) -> date:
    """Final settlement date of the reference future for `question_date`.

    The lead month references the second month on the day it goes through final settlement.
    """
    ds = sorted(final_settlement_dates)
    cands = [d for d in ds if d > question_date] if question_date in ds else [
        d for d in ds if d >= question_date
    ]
    if not cands:
        raise ValueError(f"no final settlement date on/after {question_date}")
    return cands[0]


_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_GC_RE = re.compile(
    r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s*'?(\d{2}|\d{4})\b",
    re.IGNORECASE,
)


def parse_gc_month(text: str) -> str | None:
    m = _GC_RE.search(text)
    if not m:
        return None
    month = _MONTHS.index(m.group(1).upper()) + 1
    yr = int(m.group(2))
    if yr < 100:
        yr += 2000
    return f"{yr:04d}{month:02d}"


def expiry_ts_ns(yyyymmdd: str, hhmm: str, tz_id: str) -> int:
    dt = datetime(
        int(yyyymmdd[0:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]),
        int(hhmm[0:2]), int(hhmm[2:4]), tzinfo=ZoneInfo(tz_id),
    )  # fmt: skip
    return int(dt.timestamp()) * 1_000_000_000


@dataclass(frozen=True)
class OptExpiry:
    expiry_ts_ns: int
    underlying_con_id: int
    trading_class: str
    yyyymmdd: str


@dataclass(frozen=True)
class ExpiryMatch:
    chosen: list[tuple[OptExpiry, Role]]
    mismatch: bool


def match_expiry(
    fix_ts_ns: int,
    ref_con_id: int,
    expiries: list[OptExpiry],
    tol_ns: int = 60_000_000_000,
) -> ExpiryMatch | None:
    """Pick option expiries for the fix. Only chains on the reference future are considered.

    Returns None when no chain exists on `ref_con_id`. Otherwise an exact match (within tol), or
    the bracketing expiries (possibly one-sided) flagged `mismatch=True`.
    """
    own = sorted((e for e in expiries if e.underlying_con_id == ref_con_id),
                 key=lambda e: e.expiry_ts_ns)  # fmt: skip
    if not own:
        return None
    nearest = min(own, key=lambda e: abs(e.expiry_ts_ns - fix_ts_ns))
    if abs(nearest.expiry_ts_ns - fix_ts_ns) <= tol_ns:
        return ExpiryMatch([(nearest, "match")], False)
    below = [e for e in own if e.expiry_ts_ns < fix_ts_ns]
    above = [e for e in own if e.expiry_ts_ns > fix_ts_ns]
    chosen: list[tuple[OptExpiry, Role]] = []
    if below:
        chosen.append((below[-1], "below"))
    if above:
        chosen.append((above[0], "above"))
    return ExpiryMatch(chosen, True)
