"""Gemini Predictions adapter. Data endpoints are public. The spread-weight curve is unpublished:
a quadratic (1 - d/max_spread)^2 in the distance d from the midpoint is an approximation
(scoring_verified = false). The pool is treated as shared across the pool's events unless the
config says otherwise (conservative), and the rebate is ignored (fee/rebate netting unverified)."""

from __future__ import annotations

from typing import Any

from bmm.scan.config import VenueCfg
from bmm.scan.http import GetOnlyClient
from bmm.scan.models import Book, Level, MarketMeta, Program, Side, Trade
from bmm.scan.schedule import make_schedule
from bmm.scan.venues.common import (
    classify,
    digest,
    iso_ns,
    jump_class,
    levels_sorted,
    pro_rata,
    qualifying,
    reference_quality,
)

API = "https://api.gemini.com"


def _weight(d: float, max_spread: float) -> float:
    return max(0.0, 1.0 - d / max_spread) ** 2 if max_spread > 0 else 0.0


class Gemini:
    name = "gemini"

    def __init__(self, client: GetOnlyClient, cfg: VenueCfg) -> None:
        self.c, self.cfg = client, cfg
        self._meta: dict[str, MarketMeta] = {}

    def _side(self, prog: Program) -> float:
        return prog.R_usd_hr * float(self.cfg.raw.get("side_split", 0.5))

    async def programs(self) -> list[Program]:
        raw = self.cfg.raw
        cap = int(raw.get("max_markets", 30))
        d = await self.c.get_json(
            raw.get("program_endpoint", f"{API}/v1/prediction-markets/liquidity-rewards/events"),
            {"limit": "100", "offset": "0"},
        )
        max_spread = await self._max_spread()
        out: dict[str, Program] = {}
        for ev in d["events"]:
            if len(out) >= cap:
                break
            e: dict[str, Any] = await self.c.get_json(
                f"{API}/v1/prediction-markets/events/{ev['event_ticker']}"
            )
            contracts = [c for c in e.get("contracts", []) if c.get("marketState") == "open"]
            shared = (
                1
                if raw.get("pool_scope") == "per_event"
                else max(1, int(ev.get("pool_event_count", 1)))
            )
            pool = (
                float(ev["daily_pool_usd"]) / shared / max(1, len(contracts))
            )  # per contract per day
            rate, end = pool / 24.0, iso_ns(ev.get("ends_at"))
            cat = classify(ev.get("category"), ev.get("title"))
            if (
                cat == "other"
                and "sport" in (e.get("category", "") + ev.get("category", "")).lower()
            ):
                cat = "sports"
            for c in contracts:
                sym = c["instrumentSymbol"]
                out[sym] = Program(
                    venue=self.name, market_id=sym, R_usd_hr=rate, target_size=None,
                    max_spread=max_spread, venue_min=float(raw.get("min_size", 10)), discount=None,
                    scoring="quadratic_spread_two_sided_bonus", two_sided=False,
                    maker_fee=float(raw.get("maker_fee", 0.0)), maker_rebate=0.0,
                    raw_hash=digest({"ev": ev, "c": sym}), start_ns=None, end_ns=end, pool_usd=pool,
                    schedule=make_schedule([(None, end, rate)]),
                )  # fmt: skip
                self._meta[sym] = MarketMeta(
                    market_id=sym, title=f"{e.get('title', '')}: {c.get('label', '')}", category=cat,
                    expiry_ns=iso_ns(c.get("expiryDate")), settle_source=c.get("termsAndConditionsUrl"),
                    settle_ns=iso_ns(c.get("expiryDate")), reference_quality=reference_quality(cat),
                    in_game=str(e.get("isLive")).lower() == "true",
                    jump_class=jump_class(cat, ev.get("title"), c.get("label")),
                )  # fmt: skip
        return list(out.values())[:cap]

    async def _max_spread(self) -> float:
        d = await self.c.get_json(f"{API}/v1/prediction-markets/liquidity-rewards/config")
        return float(d.get("max_spread_cents", self.cfg.raw.get("max_spread_cents", 10))) / 100.0

    async def meta(self, market_id: str) -> MarketMeta:
        return self._meta.get(market_id) or MarketMeta(
            market_id, market_id, "other", None, None, None, "none", False
        )

    async def book(self, market_id: str) -> Book:
        d = await self.c.get_json(f"{API}/v1/book/{market_id}")
        bids = [(float(x["price"]), float(x["amount"])) for x in d.get("bids", [])]
        asks = [(float(x["price"]), float(x["amount"])) for x in d.get("asks", [])]
        return Book(levels_sorted(bids, True), levels_sorted(asks, False))

    async def trades(self, market_id: str, since_ns: int) -> list[Trade]:
        d = await self.c.get_json(
            f"{API}/v1/trades/{market_id}",
            {"timestamp": str(since_ns // 10**9), "limit_trades": "500"},
        )
        # 'type' is assumed to be the taker side (trades_aggressor_verified = false).
        return [
            Trade(int(t["timestampms"]) * 10**6, float(t["price"]), float(t["amount"]), t.get("type"))
            for t in d
        ]  # fmt: skip

    def _mid(self, book: Book) -> float | None:
        return (book.bids[0].price + book.asks[0].price) / 2 if book.bids and book.asks else None

    def _levels(self, side: Side, book: Book) -> list[Level]:
        return book.bids if side == "bid" else book.asks

    def scored_depth(self, prog: Program, side: Side, book: Book) -> tuple[float, float]:
        mid, ms = self._mid(book), prog.max_spread or 0.10
        lv = self._levels(side, book)
        if mid is None or not lv:
            return 0.0, 0.0
        return sum(_weight(abs(mid - x.price), ms) * x.size for x in lv), lv[0].size

    def expected_reward(self, prog: Program, s: int, side: Side, book: Book) -> float:
        mid, lv = self._mid(book), self._levels(side, book)
        if mid is None or not lv:
            return 0.0
        w = _weight(abs(mid - lv[0].price), prog.max_spread or 0.10)
        return pro_rata(self._side(prog), w, s, self.scored_depth(prog, side, book)[0])

    def qualifying_min(self, prog: Program, side: Side, book: Book) -> int:
        return qualifying(prog, 0.0)

    def s_star_closed_form(self, prog: Program, side: Side, book: Book, c: float) -> float | None:
        return None
