"""Normalise venue quotes into per-$1 YES ladder rows.

ForecastEx is buy-only: YES=Call, NO=Put. yes_bid = best YES bid,
yes_ask = 1 - best NO bid, bid_size = YES bid size, ask_size = NO bid size.
Missing quotes (nan / inf / <= 0 / -1) become None, never price 0 or 1. A side whose displayed
size is <= 0 is treated as absent (ib_async keeps a stale price when the size drops to 0); the
raw price is still logged.
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
    if px is None or not math.isfinite(px) or px <= 0.0:
        return None
    return float(px)


def _vol(v: float | None) -> float | None:
    """Volume is a size: 0 is a valid reading; only missing, non-finite or negative is None."""
    if v is None or not math.isfinite(v) or v < 0:
        return None
    return float(v)


def _size(n: float | None) -> int:
    if n is None or not math.isfinite(n) or n < 0:
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
        volume=_vol(volume),
    )


EMPTY = RawQuote(None, None, 0, 0)
_EPS = 1e-9


@dataclass(frozen=True)
class NormRow:
    row: LadderRow
    crossed: bool
    raw_yes: RawQuote
    raw_no: RawQuote | None


def _live(px: float | None, size: int) -> float | None:
    """A price only counts as a live quote with positive displayed size."""
    return px if px is not None and size > 0 else None


def live_quote(q: RawQuote) -> RawQuote:
    """Drop bid/ask prices with no displayed size (stale) and any crossed bid/ask pair."""
    bid, ask = _live(q.bid, q.bid_size), _live(q.ask, q.ask_size)
    if bid is not None and ask is not None and bid > ask:
        bid = ask = None
    return RawQuote(bid, ask, q.bid_size if bid is not None else 0,
                    q.ask_size if ask is not None else 0, q.last, q.last_size, q.volume)  # fmt: skip


def forecastex_row(strike: float, yes: RawQuote, no: RawQuote) -> NormRow:
    for px in (yes.bid, yes.ask, no.bid, no.ask):
        if px is not None and px > 1.0 + _EPS:
            raise ScaleError(f"strike {strike}: ForecastEx price {px} > 1.0 (payout is $1)")
    yes_bid = _live(yes.bid, yes.bid_size)
    no_bid = _live(no.bid, no.bid_size)
    yes_ask = None if no_bid is None else 1.0 - no_bid
    row = LadderRow(
        strike=strike,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        bid_size=yes.bid_size if yes_bid is not None else 0,
        ask_size=no.bid_size if yes_ask is not None else 0,
    )
    # float-safe: 1.0 - 0.70 = 0.30000000000000004, so compare the sum with a tolerance
    crossed = yes_bid is not None and no_bid is not None and yes_bid + no_bid >= 1.0 - _EPS
    return NormRow(row, crossed, yes, no)


def cme_row(
    strike: float, call: RawQuote | None, put: RawQuote | None, payout_scale: float
) -> NormRow:
    """CME binary: the YES book is the call, the NO book is the put.

    yes_bid = max(call bid, 1 - put ask); yes_ask = min(call ask, 1 - put bid). Sizes come from
    the side that sets the price.
    """
    call = call or EMPTY

    def scaled(px: float | None, size: int) -> float | None:
        px = _live(px, size)
        if px is None:
            return None
        v = px / payout_scale
        if v > 1.0 + _EPS:
            raise ScaleError(
                f"strike {strike}: price {px} / scale {payout_scale} = {v} > 1; wrong payout scale"
            )
        return v

    cb, ca = scaled(call.bid, call.bid_size), scaled(call.ask, call.ask_size)
    pb = scaled(put.bid, put.bid_size) if put else None
    pa = scaled(put.ask, put.ask_size) if put else None
    for raw in (call, put):  # raw prices are range-checked even when the size is 0
        for px in (raw.bid, raw.ask) if raw else ():
            if px is not None and px / payout_scale > 1.0 + _EPS:
                raise ScaleError(f"strike {strike}: price {px} / scale {payout_scale} > 1")
    bid_c: list[tuple[float, int]] = []
    ask_c: list[tuple[float, int]] = []
    if cb is not None:
        bid_c.append((cb, call.bid_size))
    if pa is not None and put is not None:
        bid_c.append((1.0 - pa, put.ask_size))
    if ca is not None:
        ask_c.append((ca, call.ask_size))
    if pb is not None and put is not None:
        ask_c.append((1.0 - pb, put.bid_size))
    yes_bid, bid_size = max(bid_c, key=lambda t: t[0]) if bid_c else (None, 0)
    yes_ask, ask_size = min(ask_c, key=lambda t: t[0]) if ask_c else (None, 0)
    row = LadderRow(strike, yes_bid, yes_ask, bid_size, ask_size)
    crossed = yes_bid is not None and yes_ask is not None and yes_bid >= yes_ask - _EPS
    return NormRow(row, crossed, call, put)


def check_ladder_scale(rows: list[NormRow], payout_scale: float) -> None:
    """Catch a too-large scale: with scale > 1 every raw price on a real ladder exceeds 1."""
    if payout_scale <= 1.0:
        return
    prices = [
        px
        for r in rows
        for q in (r.raw_yes, r.raw_no)
        if q is not None
        for px in (q.bid, q.ask)
        if px is not None
    ]
    if prices and max(prices) <= 1.0 + _EPS:
        raise ScaleError(
            f"payout_scale {payout_scale} but every raw price is <= 1 (max {max(prices)}); "
            "scale looks too large"
        )
