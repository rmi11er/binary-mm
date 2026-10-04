"""Implied vol, ATM sigma and strike window (calendar-year T, undiscounted Black-76)."""

from __future__ import annotations

import math
from collections.abc import Sequence

from scipy.optimize import brentq

from bmm.spotcheck.analyze import OptQuote

_LO, _HI = 1e-4, 5.0


def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def black76_call(F: float, K: float, T: float, sig: float) -> float:
    """Undiscounted Black-76 call. Local copy: importing bmm.pricing would pull in
    bmm.pricing.quoting (order construction) via the package __init__, which the spotcheck
    must never load. Equality with bmm.pricing.black76_call is pinned in test_vol.py."""
    v = sig * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v) / v
    return F * _ncdf(d1) - K * _ncdf(d1 - v)


def implied_vol(F: float, K: float, T: float, price: float) -> float | None:
    """Black-76 IV by brentq on [1e-4, 5]; None if price is outside the no-arbitrage range."""
    if not (F > 0 and K > 0 and T > 0) or math.isnan(price):
        return None
    intrinsic = max(F - K, 0.0)
    if price <= intrinsic or price >= F:
        return None

    def f(s: float) -> float:
        return float(black76_call(F, K, T, s)) - price

    if f(_LO) * f(_HI) > 0:
        return None  # price not attainable inside the vol bracket
    return float(brentq(f, _LO, _HI, xtol=1e-12, rtol=1e-12))


def atm_sigma(chain: list[OptQuote], F: float, T: float) -> float | None:
    """IV of the call mid at the strike nearest F, skipping unusable quotes.

    Quotes with bid <= 0, ask < bid or non-finite prices are ignored; if the nearest valid
    strike has no solvable IV, the next-nearest is tried.
    """
    good = [
        q
        for q in chain
        if math.isfinite(q.call_bid) and math.isfinite(q.call_ask)
        and q.call_bid > 0 and q.call_ask >= q.call_bid
    ]  # fmt: skip
    for q in sorted(good, key=lambda c: abs(c.strike - F)):
        iv = implied_vol(F, q.strike, T, q.mid)
        if iv is not None:
            return iv
    return None


def strike_window(
    F: float,
    sigma: float,
    T: float,
    n_sd: float = 4.0,
    ladder_strikes: Sequence[float] | None = None,
    listed: Sequence[float] | None = None,
) -> tuple[float, float]:
    """F*exp(+-n_sd*sigma*sqrt(T)), widened to cover the venue ladder's strike range and, when
    `listed` option strikes are given, extended one listed strike beyond each edge so every
    ladder strike is bracketed by option strikes."""
    w = n_sd * sigma * math.sqrt(T)
    lo, hi = F * math.exp(-w), F * math.exp(w)
    if ladder_strikes:
        lo, hi = min(lo, min(ladder_strikes)), max(hi, max(ladder_strikes))
    if listed:
        below = [k for k in listed if k < lo]
        above = [k for k in listed if k > hi]
        lo = max(below) if below else lo
        hi = min(above) if above else hi
    return lo, hi


def year_frac(from_ns: int, to_ns: int) -> float:
    """Calendar-year fraction. Within 1 s of zero clamps to 1e-9; a materially negative
    interval (expiry already passed) raises ValueError."""
    d = to_ns - from_ns
    if d < -1_000_000_000:
        raise ValueError(f"negative interval: {d / 1e9:.1f}s")
    return max(d / (365 * 86400 * 1e9), 1e-9)
