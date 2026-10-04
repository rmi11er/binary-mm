"""Fee / rebate schedules in the common p(1-p) form.

Figures are as published at time of research (see research/venue_facts.md).
Treat them as config and re-verify before use.
"""
from __future__ import annotations

import math


def p1p_fee(price: float, contracts: float, rate: float) -> float:
    """Generic fee = rate * contracts * p * (1-p)."""
    if not 0.0 <= price <= 1.0:
        raise ValueError("price must be in [0,1]")
    return rate * contracts * price * (1.0 - price)


def kalshi_taker_fee(price: float, contracts: int, rate: float = 0.07) -> float:
    """Kalshi-style taker fee rounded UP to the cent (verify current schedule)."""
    raw = p1p_fee(price, contracts, rate)
    return math.ceil(raw * 100 - 1e-9) / 100


def pascal_fees(
    price: float, contracts: float, taker_rate: float = 0.02, maker_share: float = 0.25
) -> tuple[float, float]:
    """(taker_fee, maker_rebate) per Pascal docs defaults."""
    t = p1p_fee(price, contracts, taker_rate)
    return t, t * maker_share


def gemini_maker_rebate(
    price: float, contracts: float, taker_rate: float = 0.07, rebate_rate: float = 0.30
) -> float:
    """Gemini Predictions: rebate_rate * taker_rate * C * P * (1-P)."""
    return rebate_rate * p1p_fee(price, contracts, taker_rate)
