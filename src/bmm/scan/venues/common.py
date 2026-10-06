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
    "energy": r"\b(oil|wti|brent|natgas|natural gas|gasoline|crude)\b",
    "rates": r"\b(fed|fomc|treasury|yield|sofr|rate cut|rate hike|cpi|inflation)\b",
    "fx": r"\b(eur|usd|jpy|gbp|fx|dollar|yen|euro)\b",
    "equity_index": r"\b(s&p|spx|nasdaq|ndx|dow|russell)\b",
    "crypto": r"\b(btc|eth|bitcoin|ether|solana|crypto)\b",
    "sports": r"\b(nfl|nba|mlb|nhl|cfb|ncaa|football|basketball|baseball|soccer|tennis|ufc)\b",
    "geopolitics": (
        r"\b(hormuz|strait|war|ceasefire|cease-fire|sanctions?|invasion|invade|missile|nato|ukraine"
        r"|russia|iran|israel|gaza|taiwan|coup|embargo|blockade)\b"
    ),
    "politics": (
        r"\b(election|president|presidential|senate|congress|governor|primary|nominee|trump|biden"
        r"|vance|democrat|republican|vote|impeach|cabinet|mayor|parliament|prime minister)\b"
    ),
}
HIGH_JUMP_CATS = {"politics", "geopolitics"}
NEWS_DRIVEN = re.compile(
    r"\b(announce|resign|indict|arrest|verdict|ruling|nominate|merger|acquire|bankrupt|default"
    r"|mention|say|says|tweet|post)\b"
)
DOMAIN_CATS = {"metals", "energy", "rates", "fx", "equity_index"}


_FREQ = r"(?:15M|H|D|W|MON|EOY|EOM|MAX|MIN|Y)?"
_METALS_TICKER = re.compile(
    r"^(?:KX)?(?:GOLD|SILVER|COPPER|PLATINUM|PALLADIUM)" + _FREQ + r"(?:-|$)"
)
# price series only: KXGOLDCARD, KXCOPPEROPEN, KXGOLDENBACHELOR do not match
_METAL_WORD = re.compile(r"\b(gold|silver|copper|platinum|palladium)\b")
_METAL_CONTEXT = re.compile(
    r"\b(price|prices|close|closes|above|below|settle|settles|range|how high|how low|hit|hits|reach|hourly|daily"
    r"|weekly|monthly|15-minute|15 minute|per ounce|per pound|ounce|oz|futures|higher|lower)\b|\$"
)
_METAL_NEG = re.compile(
    r"\b(cup|card|cards|bachelor|mountain|cable|withdrawals?|vs\.?|versus|medal|rush|coast|glove|gloves|standard"
    r"|album|ring|jewelry|win|wins)\b"
)
_FX = "USD|EUR|JPY|GBP|AUD|CAD|CHF|NZD|CNH"
_END = r"(?:-|$)"
_TICKER_RULES = [  # explicit series roots (+ optional frequency): "KXDOWNTOWN" / "KXFEDCHAIRNOM" must not match
    ("metals", _METALS_TICKER),
    ("fx", re.compile(r"^KX(?:" + _FX + r"){2}" + _FREQ + _END)),
    (
        "energy",
        re.compile(r"^KX(?:WTI|BRENT|OIL|NATGAS|CRUDE|GASOLINE|HENRYHUB|AAAGAS)" + _FREQ + _END),
    ),
    (
        "rates",
        re.compile(
            r"^KX(?:FED|FEDDECISION|FOMC|CPI|CPIYOY|CORECPI|TNOTE|TBILL|SOFR|RATECUT|RATECUTCOUNT|RATEHIKE|YIELD|10Y|2Y)"
            + _FREQ
            + _END
        ),
    ),
    (
        "equity_index",
        re.compile(
            r"^KX(?:INX|INXU|INXD|INXW|INXY|NASDAQ100|NASDAQ100U|NASDAQ100D|NDX|SPX|SP500|DJIA)"
            + _FREQ
            + _END
        ),
    ),
]


def ticker_category(ticker: str) -> str | None:
    """Domain category from a venue ticker alone (cheap, used to triage before any fetch)."""
    t = ticker.upper()
    return next((cat for cat, pat in _TICKER_RULES if pat.match(t)), None)


def _is_metals(texts: tuple[str | None, ...]) -> bool:
    for t in texts:
        if t and " " not in t.strip() and _METALS_TICKER.match(t.strip().upper()):
            return True
    blob = " ".join(t for t in texts if t and " " in t.strip()).lower()  # free-text titles only
    return bool(
        blob
        and _METAL_WORD.search(blob)
        and _METAL_CONTEXT.search(blob)
        and not _METAL_NEG.search(blob)
    )


def classify(*texts: str | None) -> str:
    if _is_metals(texts):
        return "metals"
    blob = " ".join(t for t in texts if t).lower()
    for cat, pat in DOMAIN_KEYWORDS.items():
        if cat != "metals" and re.search(pat, blob):
            return cat
    return "other"


def jump_class(category: str, *texts: str | None) -> str:
    """ "high" when resolution is news-driven (politics, geopolitics, announcements): the price can
    gap to 0/1 on one headline. Everything else is "normal" and uses the empirical 1h tail."""
    blob = " ".join(t for t in texts if t).lower()
    return "high" if category in HIGH_JUMP_CATS or NEWS_DRIVEN.search(blob) else "normal"


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
