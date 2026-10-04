"""Helpers shared by the venue adapters. All venue rules cited here live in config/scan_venues.toml
and research/venue_facts.md; anything unverified there is an approximation here too."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Any

from bmm.scan.models import Level, Program

DOMAIN_KEYWORDS = {
    "metals": r"\b(gold|silver|copper|platinum|palladium)\b",
    "energy": r"\b(oil|wti|brent|natgas|natural gas|gasoline|crude|hormuz)\b",
    "rates": r"\b(fed|fomc|treasury|yield|sofr|rate cut|rate hike|cpi|inflation)\b",
    "fx": r"\b(eur|usd|jpy|gbp|fx|dollar|yen|euro)\b",
    "equity_index": r"\b(s&p|spx|nasdaq|ndx|dow|russell)\b",
    "crypto": r"\b(btc|eth|bitcoin|ether|solana|crypto)\b",
    "sports": r"\b(nfl|nba|mlb|nhl|cfb|ncaa|football|basketball|baseball|soccer|tennis|ufc)\b",
}
DOMAIN_CATS = {"metals", "energy", "rates", "fx", "equity_index"}


def classify(*texts: str | None) -> str:
    blob = " ".join(t for t in texts if t).lower()
    for cat, pat in DOMAIN_KEYWORDS.items():
        if re.search(pat, blob):
            return cat
    return "other"


def reference_quality(category: str, venue_names_source: bool = False) -> str:
    if venue_names_source:
        return "venue_ref"
    return "model_needed" if category in DOMAIN_CATS else "none"


def iso_ns(s: str | None) -> int | None:
    if not s:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    return int(datetime.fromisoformat(s).astimezone(UTC).timestamp()) * 10**9 + (
        datetime.fromisoformat(s).microsecond * 1000
    )


def digest(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def levels_sorted(raw: list[tuple[float, float]], desc: bool) -> list[Level]:
    return [Level(p, q) for p, q in sorted(raw, key=lambda x: x[0], reverse=desc) if q > 0]


def geo_walk(
    levels: list[Level],
    target: float | None,
    discount: float,
    tick: float,
    ref: float | None = None,
) -> float:
    """Competing score: walk outward from the best level counting size until `target` is reached,
    weighting each level discount^(ticks beyond the reference price; best price by default)."""
    if not levels:
        return 0.0
    best = levels[0].price
    ref_dist = abs(ref - best) if ref is not None else 0.0
    remaining = math.inf if target is None else target
    score = 0.0
    for lv in levels:
        take = min(lv.size, remaining)
        ticks = max(0.0, (abs(lv.price - best) - ref_dist) / tick)
        score += discount ** round(ticks, 6) * take
        remaining -= take
        if remaining <= 0:
            break
    return score


def pro_rata(r_side: float, weight: float, s: int, d: float) -> float:
    if s <= 0 or weight <= 0:
        return 0.0
    return r_side * weight * s / (weight * s + d)


def qualifying(prog: Program, existing_side_depth: float) -> int:
    """max(venue_min, target - existing side depth), as an integer contract count."""
    need = math.ceil((prog.target_size or 0.0) - existing_side_depth)
    return int(max(math.ceil(prog.venue_min), need))
