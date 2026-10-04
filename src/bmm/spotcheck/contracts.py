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
_MONTH_RE = (
    r"JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUN(?:E)?|JUL(?:Y)?|AUG(?:UST)?"
    r"|SEP(?:T(?:EMBER)?)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?"
)
_GC_RE = re.compile(rf"\b({_MONTH_RE})[\s\-'/]*(\d{{4}}|\d{{2}})\b", re.IGNORECASE)
_DAY_TAIL = re.compile(r"\s*,?\s*\d{4}\b")  # "Oct 26, 2026": the 26 is a day, not a year
_GOLD_FUT = re.compile(r"gold\s+futures?", re.IGNORECASE)

ASSUMPTIONS: dict[str, str] = {
    "forecastex-ndx": (
        "fix_ct 15:00 CT is ASSUMED equal to the ES fix; the NQ settlement window is [?] in "
        "research/venue_facts.md (NQ EC settlement window)"
    ),
}


def parse_gc_month(text: str, question_date: date) -> str | None:
    """Contract month (YYYYMM) of the gold future named in `text`, or None if absent/ambiguous.

    Only whole month names or 3-letter abbreviations followed by a 2- or 4-digit year are
    accepted. A 2-digit number followed by ", YYYY" is a day of month, not a year. Years outside
    [question year - 1, question year + 3] are rejected. If several distinct months remain,
    prefer the one adjacent to "gold futures"; otherwise return None rather than guess.
    """
    qy = question_date.year
    found: list[tuple[str, int, int]] = []
    for m in _GC_RE.finditer(text):
        digits = m.group(2)
        if len(digits) == 2 and _DAY_TAIL.match(text, m.end()):
            continue
        yr = int(digits) + (2000 if len(digits) == 2 else 0)
        if not (qy - 1 <= yr <= qy + 3):
            continue
        month = _MONTHS.index(m.group(1).upper()[:3]) + 1
        found.append((f"{yr:04d}{month:02d}", m.start(), m.end()))
    distinct = {f[0] for f in found}
    if len(distinct) <= 1:
        return next(iter(distinct), None)
    anchors = [g.span() for g in _GOLD_FUT.finditer(text)]
    near = {
        v
        for v, a, b in found
        if any(a - e <= 25 and s - b <= 25 for s, e in anchors)  # span distance
    }
    return next(iter(near)) if len(near) == 1 else None


_TZ_ALIASES = {
    "CST": "America/Chicago", "CDT": "America/Chicago", "CT": "America/Chicago",
    "EST": "America/New_York", "EDT": "America/New_York", "ET": "America/New_York",
    "UTC": "UTC", "GMT": "UTC",
}  # fmt: skip


def _zone(tz_id: str) -> ZoneInfo:
    t = tz_id.strip()
    head = t.split()[0].upper() if t else ""
    name = _TZ_ALIASES.get(head) or (
        "America/Chicago" if "central" in t.lower() else
        "America/New_York" if "eastern" in t.lower() else t
    )  # fmt: skip
    try:
        return ZoneInfo(name)
    except Exception as e:  # ZoneInfoNotFoundError, ValueError
        raise ValueError(f"unrecognised IB timezone id {tz_id!r}") from e


def expiry_ts_ns(yyyymmdd: str, hhmm: str, tz_id: str) -> int:
    """`hhmm` may be "HHMM", "HH:MM" or "HH:MM:SS"."""
    digits = hhmm.replace(":", "").strip()
    if len(digits) not in (4, 6) or not digits.isdigit():
        raise ValueError(f"bad time {hhmm!r}")
    sec = int(digits[4:6]) if len(digits) == 6 else 0
    dt = datetime(
        int(yyyymmdd[0:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]),
        int(digits[0:2]), int(digits[2:4]), sec, tzinfo=_zone(tz_id),
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
    max_gap_ns: int = 2 * 86_400_000_000_000,
) -> ExpiryMatch | None:
    """Pick option expiries for the fix. Only chains on the reference future are considered.

    Returns None when no chain exists on `ref_con_id` (or none within `max_gap_ns` of the fix).
    Otherwise an exact match (within tol), or the bracketing expiries (possibly one-sided)
    flagged `mismatch=True`.
    """
    # deterministic order across trading classes: (expiry, class, date)
    own = sorted((e for e in expiries if e.underlying_con_id == ref_con_id),
                 key=lambda e: (e.expiry_ts_ns, e.trading_class, e.yyyymmdd))  # fmt: skip
    if not own:
        return None
    nearest = min(own, key=lambda e: abs(e.expiry_ts_ns - fix_ts_ns))  # first in sort order on ties
    if abs(nearest.expiry_ts_ns - fix_ts_ns) <= tol_ns:
        return ExpiryMatch([(nearest, "match")], False)
    below = [e for e in own if 0 < fix_ts_ns - e.expiry_ts_ns <= max_gap_ns]
    above = [e for e in own if 0 < e.expiry_ts_ns - fix_ts_ns <= max_gap_ns]
    chosen: list[tuple[OptExpiry, Role]] = []
    if below:
        chosen.append((below[-1], "below"))
    if above:
        chosen.append((above[0], "above"))
    return ExpiryMatch(chosen, True) if chosen else None
