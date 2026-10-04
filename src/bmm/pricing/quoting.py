"""Quote construction and venue translation."""
from __future__ import annotations

import math
from dataclasses import dataclass

TICK = 0.01


@dataclass(frozen=True)
class Quote:
    yes_bid: float | None
    yes_ask: float | None

    def width(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return round(self.yes_ask - self.yes_bid, 10)


def _floor_tick(x: float, tick: float = TICK) -> float:
    return round(math.floor(x / tick + 1e-9) * tick, 10)


def _ceil_tick(x: float, tick: float = TICK) -> float:
    return round(math.ceil(x / tick - 1e-9) * tick, 10)


def two_sided_quote(
    fair: float,
    half_spread: float,
    inventory: float = 0.0,
    skew_per_contract: float = 0.0,
    min_price: float = 0.01,
    max_price: float = 0.99,
    tick: float = TICK,
) -> Quote:
    """Reservation-price quoting (Avellaneda-Stoikov flavoured, linear inventory skew).

    reservation = fair - inventory * skew_per_contract   (long YES => shade both quotes down)
    bid = floor(reservation - h), ask = ceil(reservation + h), clipped to [min_price, max_price].
    A side is dropped (None) when it falls outside the price band.
    """
    if not 0.0 < fair < 1.0:
        raise ValueError("fair must be in (0,1)")
    if half_spread < 0:
        raise ValueError("half_spread must be non-negative")
    r = fair - inventory * skew_per_contract
    bid = _floor_tick(r - half_spread, tick)
    ask = _ceil_tick(r + half_spread, tick)
    bid_out = bid if min_price <= bid <= max_price else None
    ask_out = ask if min_price <= ask <= max_price else None
    if bid_out is not None and ask_out is not None and bid_out >= ask_out:
        return Quote(None, None)
    return Quote(bid_out, ask_out)


def to_forecastex_orders(q: Quote) -> list[dict]:
    """ForecastEx is BUY-ONLY: YES = Call right, NO = Put right (IBKR models them as OPT).

    Offering YES at a  <=>  bidding NO at (1 - a). Returns limit BUY order intents.
    Positions flatten by buying the opposite contract (confirm netting/collateral in Phase 0).
    """
    orders: list[dict] = []
    if q.yes_bid is not None:
        orders.append({"action": "BUY", "right": "C", "limit": q.yes_bid})
    if q.yes_ask is not None:
        orders.append({"action": "BUY", "right": "P", "limit": round(1.0 - q.yes_ask, 10)})
    return orders
