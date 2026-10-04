"""Normalise venue quotes into per-$1 YES ladder rows.

ForecastEx is buy-only: YES=Call, NO=Put. yes_bid = best YES bid,
yes_ask = 1 - best NO bid, bid_size = YES bid size, ask_size = NO bid size.
Missing quotes (nan / <= 0 / -1) become None, never price 0 or 1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from bmm.spotcheck.analyze import LadderRow


class ScaleError(ValueError):
    """Normalised price exceeds 1.0: payout scale is wrong."""


@dataclass(frozen=True)
class RawQuote:
    """One IB ticker, already in venue price units."""

    bid: float | None
    ask: float | None
    bid_size: int
    ask_size: int
    last: float | None = None
    last_size: int = 0
    volume: float | None = None


def clean(px: float | None) -> float | None:
    if px is None or math.isnan(px) or px <= 0.0:
        return None
    return float(px)


def _size(n: float | None) -> int:
    if n is None or (isinstance(n, float) and math.isnan(n)) or n < 0:
        return 0
    return int(n)


def raw_quote(
    bid: float | None,
    ask: float | None,
    bid_size: int,
    ask_size: int,
    last: float | None = None,
    last_size: int = 0,
    volume: float | None = None,
) -> RawQuote:
    return RawQuote(
        bid=clean(bid),
        ask=clean(ask),
        bid_size=_size(bid_size),
        ask_size=_size(ask_size),
        last=clean(last),
        last_size=_size(last_size),
        volume=clean(volume),
    )


@dataclass(frozen=True)
class NormRow:
    row: LadderRow
    crossed: bool
    raw_yes: RawQuote
    raw_no: RawQuote | None


def _crossed(yes_bid: float | None, yes_ask: float | None) -> bool:
    return yes_bid is not None and yes_ask is not None and yes_bid >= yes_ask


def forecastex_row(strike: float, yes: RawQuote, no: RawQuote) -> NormRow:
    yes_bid = yes.bid
    yes_ask = None if no.bid is None else 1.0 - no.bid
    row = LadderRow(
        strike=strike,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        bid_size=yes.bid_size if yes_bid is not None else 0,
        ask_size=no.bid_size if yes_ask is not None else 0,
    )
    return NormRow(row, _crossed(yes_bid, yes_ask), yes, no)


def cme_row(strike: float, q: RawQuote, payout_scale: float) -> NormRow:
    def scaled(px: float | None) -> float | None:
        if px is None:
            return None
        v = px / payout_scale
        if v > 1.0 + 1e-9:
            raise ScaleError(
                f"strike {strike}: price {px} / scale {payout_scale} = {v} > 1; wrong payout scale"
            )
        return v

    yes_bid, yes_ask = scaled(q.bid), scaled(q.ask)
    row = LadderRow(
        strike=strike,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        bid_size=q.bid_size if yes_bid is not None else 0,
        ask_size=q.ask_size if yes_ask is not None else 0,
    )
    return NormRow(row, _crossed(yes_bid, yes_ask), q, None)
