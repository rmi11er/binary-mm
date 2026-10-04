"""Implied vol, ATM sigma and strike window (calendar-year T, undiscounted Black-76)."""

from __future__ import annotations

import math

from scipy.optimize import brentq

from bmm.pricing import black76_call
from bmm.spotcheck.analyze import OptQuote

_LO, _HI = 1e-4, 5.0


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
    """IV of the call mid at the strike nearest F."""
    if not chain:
        return None
    q = min(chain, key=lambda c: abs(c.strike - F))
    return implied_vol(F, q.strike, T, q.mid)


def strike_window(F: float, sigma: float, T: float, n_sd: float = 4.0) -> tuple[float, float]:
    w = n_sd * sigma * math.sqrt(T)
    return F * math.exp(-w), F * math.exp(w)


def year_frac(from_ns: int, to_ns: int) -> float:
    return max((to_ns - from_ns) / (365 * 86400 * 1e9), 1e-9)
