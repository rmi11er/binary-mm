"""Plain data types shared by the scanner. See docs/spec/60-rewards-scanner.md."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

Side = Literal["bid", "ask"]


class VenueNotPublic(Exception):
    """The venue's data needs credentials, is not JSON, or is otherwise not public."""


class VenueRateLimited(VenueNotPublic):
    """HTTP 429 persisted after backoff; the market or venue is skipped for this scan."""


@dataclass(frozen=True)
class Program:
    venue: str
    market_id: str
    R_usd_hr: float
    target_size: float | None
    max_spread: float | None
    venue_min: float
    discount: float | None
    scoring: str
    two_sided: bool
    maker_fee: float
    maker_rebate: float
    raw_hash: str


@dataclass(frozen=True)
class Level:
    price: float
    size: float
    top_holder_share: float | None = None


@dataclass(frozen=True)
class Book:
    bids: list[Level]  # best (highest) first
    asks: list[Level]  # best (lowest) first
    shares_traded: float | None = None  # cumulative contracts traded, where the venue exposes it
    opened_ns: int | None = None


@dataclass(frozen=True)
class Trade:
    ts_ns: int
    price: float
    size: float
    aggressor: str | None  # "buy" | "sell" | None


@dataclass(frozen=True)
class MarketMeta:
    market_id: str
    title: str
    category: str
    expiry_ns: int | None
    settle_source: str | None
    settle_ns: int | None
    reference_quality: str  # venue_ref | model_needed | none
    in_game: bool
    jump_class: str = "normal"  # high = news-driven resolution; see venues/common.jump_class


class VenueAdapter(Protocol):
    name: str

    async def programs(self) -> list[Program]: ...
    async def meta(self, market_id: str) -> MarketMeta: ...
    async def book(self, market_id: str) -> Book: ...
    async def trades(self, market_id: str, since_ns: int) -> list[Trade]: ...
    def expected_reward(self, prog: Program, s: int, side: Side, book: Book) -> float: ...
    def qualifying_min(self, prog: Program, side: Side, book: Book) -> int: ...
    def s_star_closed_form(
        self, prog: Program, side: Side, book: Book, c: float
    ) -> float | None: ...
    def scored_depth(self, prog: Program, side: Side, book: Book) -> tuple[float, float]: ...


JSON = Any
