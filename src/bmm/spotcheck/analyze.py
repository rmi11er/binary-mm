"""One-off spot check: are venue binary ladders mispriced vs the options surface by more than fees?

Model-free core. For a digital D(K) = P(F_T > K) (undiscounted; intraday df ~ 1):

    slope(a, b) = (C(a) - C(b)) / (b - a)  = average of D over [a, b].

D is non-increasing in K, so for adjacent listed option strikes:
    any interval ending at or below K  -> its average >= D(K)   (UPPER bound)
    any interval starting at or above K -> its average <= D(K)  (LOWER bound)

Using option bid/ask instead of mids gives *replication-grade* bounds:
    upper = (C_ask(a) - C_bid(b)) / (b - a)   for b <= K
    lower = (C_bid(c) - C_ask(d)) / (d - c)   for c >= K
A venue YES bid above `upper` (or ask below `lower`) is mispriced beyond any model choice.
The point estimate interpolates mid-based interval slopes (placed at interval midpoints) to K.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OptQuote:
    strike: float
    call_bid: float
    call_ask: float

    @property
    def mid(self) -> float:
        return 0.5 * (self.call_bid + self.call_ask)


@dataclass(frozen=True)
class LadderRow:
    strike: float
    yes_bid: float | None  # per $1 payout; normalise CME $100 contracts before passing
    yes_ask: float | None
    bid_size: int = 0
    ask_size: int = 0


@dataclass(frozen=True)
class FairBand:
    strike: float
    fair: float
    lower: float  # replication-grade lower bound on D(K)
    upper: float  # replication-grade upper bound on D(K)


@dataclass(frozen=True)
class EdgeRow:
    strike: float
    fair: float
    lower: float
    upper: float
    yes_bid: float | None
    yes_ask: float | None
    mid_gap: float | None          # venue mid - fair (+ => venue rich)
    take_buy_edge: float | None    # fair - ask - fee      (buy YES now)
    take_sell_edge: float | None   # bid - fair - fee      (sell YES now / buy NO)
    arb_buy_edge: float | None     # lower - ask - fee     (model-free)
    arb_sell_edge: float | None    # bid - upper - fee     (model-free)
    bid_size: int
    ask_size: int


def _usable(chain: list[OptQuote], max_rel_width: float) -> list[OptQuote]:
    out = []
    for q in sorted(chain, key=lambda x: x.strike):
        if q.call_ask <= 0 or q.call_ask < q.call_bid or q.call_bid < 0:
            continue  # crossed or empty
        if (q.call_ask - q.call_bid) > max_rel_width * max(q.mid, 1e-9) and q.mid > 0.5:
            continue  # very wide quote on a non-trivial premium -> skip
        out.append(q)
    return out


def fair_band(chain: list[OptQuote], K: float, max_rel_width: float = 0.5) -> FairBand | None:
    """Point estimate + no-arb bounds for D(K). None if K is not bracketed by usable strikes."""
    qs = _usable(chain, max_rel_width)
    if len(qs) < 3:
        return None
    below = [i for i in range(len(qs) - 1) if qs[i + 1].strike <= K]
    above = [i for i in range(len(qs) - 1) if qs[i].strike >= K]
    if not below or not above:
        return None
    i_b, i_a = below[-1], above[0]
    a, b = qs[i_b], qs[i_b + 1]
    c, d = qs[i_a], qs[i_a + 1]
    upper = (a.call_ask - b.call_bid) / (b.strike - a.strike)
    lower = (c.call_bid - d.call_ask) / (d.strike - c.strike)
    # point estimate: mid slopes at interval midpoints, linear interp to K
    s_b, x_b = (a.mid - b.mid) / (b.strike - a.strike), 0.5 * (a.strike + b.strike)
    s_a, x_a = (c.mid - d.mid) / (d.strike - c.strike), 0.5 * (c.strike + d.strike)
    w = 0.0 if x_a == x_b else (K - x_b) / (x_a - x_b)
    fair = s_b + w * (s_a - s_b)
    clip = lambda v: min(max(v, 0.0), 1.0)  # noqa: E731
    return FairBand(K, clip(fair), clip(lower), clip(upper))


def edges(ladder: list[LadderRow], chain: list[OptQuote], fee_per_side: float,
          max_rel_width: float = 0.5) -> list[EdgeRow]:
    """Per-strike edge after a flat per-contract fee (ForecastEx: 0.01 per side)."""
    rows: list[EdgeRow] = []
    for r in ladder:
        fb = fair_band(chain, r.strike, max_rel_width)
        if fb is None:
            continue
        mid = (0.5 * (r.yes_bid + r.yes_ask)
               if r.yes_bid is not None and r.yes_ask is not None else None)
        rows.append(EdgeRow(
            strike=r.strike, fair=fb.fair, lower=fb.lower, upper=fb.upper,
            yes_bid=r.yes_bid, yes_ask=r.yes_ask,
            mid_gap=None if mid is None else mid - fb.fair,
            take_buy_edge=None if r.yes_ask is None else fb.fair - r.yes_ask - fee_per_side,
            take_sell_edge=None if r.yes_bid is None else r.yes_bid - fb.fair - fee_per_side,
            arb_buy_edge=None if r.yes_ask is None else fb.lower - r.yes_ask - fee_per_side,
            arb_sell_edge=None if r.yes_bid is None else r.yes_bid - fb.upper - fee_per_side,
            bid_size=r.bid_size, ask_size=r.ask_size,
        ))
    return rows


def capturable_dollars(rows: list[EdgeRow], threshold: float = 0.0, model_free: bool = False) -> float:
    """$ edge immediately takeable at displayed size, counting only edges > threshold."""
    total = 0.0
    for e in rows:
        buy = e.arb_buy_edge if model_free else e.take_buy_edge
        sell = e.arb_sell_edge if model_free else e.take_sell_edge
        if buy is not None and buy > threshold:
            total += buy * e.ask_size
        if sell is not None and sell > threshold:
            total += sell * e.bid_size
    return total
