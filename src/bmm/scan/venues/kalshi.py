"""Kalshi adapter. Incentives, orderbook and trades are public. A YES ask at a is a NO bid at 1-a,
so asks are derived from the NO bids."""

from __future__ import annotations

from typing import Any

from bmm.scan.config import VenueCfg
from bmm.scan.http import GetOnlyClient
from bmm.scan.models import Book, Level, MarketMeta, Program, Side, Trade
from bmm.scan.venues.common import (
    classify,
    digest,
    geo_walk,
    iso_ns,
    levels_sorted,
    pro_rata,
    qualifying,
    reference_quality,
)

API = "https://external-api.kalshi.com/trade-api/v2"


class Kalshi:
    name = "kalshi"

    def __init__(self, client: GetOnlyClient, cfg: VenueCfg) -> None:
        self.c, self.cfg = client, cfg

    def _side(self, prog: Program) -> float:
        return prog.R_usd_hr * float(self.cfg.raw.get("side_split", 0.5))

    async def programs(self) -> list[Program]:
        d = await self.c.get_json(
            self.cfg.raw.get("program_endpoint", f"{API}/incentive_programs"),
            {"status": "active", "type": "liquidity"},
        )
        out: list[Program] = []
        for p in d["incentive_programs"][: int(self.cfg.raw.get("max_markets", 100))]:
            start, end = iso_ns(p["start_date"]), iso_ns(p["end_date"])
            if not start or not end or end <= start:
                continue
            hours = (end - start) / 3.6e12
            out.append(
                Program(
                    venue=self.name, market_id=p["market_ticker"],
                    R_usd_hr=(float(p["period_reward"]) / 1e4) / hours,
                    target_size=float(p["target_size_fp"]), max_spread=None,
                    venue_min=float(self.cfg.raw.get("venue_min", 1.0)),
                    discount=float(p["discount_factor_bps"]) / 1e4,
                    scoring="geometric_target", two_sided=True,
                    maker_fee=float(self.cfg.raw.get("maker_fee", 0.0)),
                    maker_rebate=float(self.cfg.raw.get("maker_rebate", 0.0)),
                    raw_hash=digest(p),
                )
            )  # fmt: skip
        return out

    async def meta(self, market_id: str) -> MarketMeta:
        m: dict[str, Any] = (await self.c.get_json(f"{API}/markets/{market_id}"))["market"]
        cat = classify(m.get("title"), market_id, m.get("rules_primary"))
        exp = iso_ns(m.get("expected_expiration_time") or m.get("close_time"))
        return MarketMeta(
            market_id=market_id, title=m.get("title", market_id), category=cat, expiry_ns=exp,
            settle_source=(m.get("rules_primary") or "")[:200] or None, settle_ns=exp,
            reference_quality=reference_quality(cat), in_game=False,
        )  # fmt: skip

    async def book(self, market_id: str) -> Book:
        d = (await self.c.get_json(f"{API}/markets/{market_id}/orderbook"))["orderbook_fp"]
        yes = [(float(p), float(q)) for p, q in d.get("yes_dollars", [])]
        no = [(1.0 - float(p), float(q)) for p, q in d.get("no_dollars", [])]
        return Book(levels_sorted(yes, True), levels_sorted(no, False))

    async def trades(self, market_id: str, since_ns: int) -> list[Trade]:
        d = await self.c.get_json(
            f"{API}/markets/trades",
            {"ticker": market_id, "min_ts": str(since_ns // 10**9), "limit": "1000"},
        )
        # taker_outcome_side "yes" = the taker bought YES (lifted the ask) -> "buy" in YES terms.
        return [
            Trade(
                iso_ns(t["created_time"]) or 0, float(t["yes_price_dollars"]), float(t["count_fp"]),
                "buy" if t.get("taker_outcome_side") == "yes" else "sell",
            )
            for t in d.get("trades", [])
        ]  # fmt: skip

    def _levels(self, side: Side, book: Book) -> list[Level]:
        return book.bids if side == "bid" else book.asks

    def scored_depth(self, prog: Program, side: Side, book: Book) -> tuple[float, float]:
        lv = self._levels(side, book)
        if not lv:
            return 0.0, 0.0
        target = prog.target_size or 0.0
        cum, ref = 0.0, lv[-1].price
        for x in lv:  # Reference Price: first level where cumulative size reaches target/5
            cum += x.size
            if cum >= target / 5:
                ref = x.price
                break
        tick = float(self.cfg.raw.get("scoring_tick", 0.01))
        return geo_walk(lv, target, prog.discount or 1.0, tick, ref), lv[0].size

    def expected_reward(self, prog: Program, s: int, side: Side, book: Book) -> float:
        # Each side is normalised to 1.0 of a 2.0 snapshot score -> R/2 per side. Our order at the
        # best price is at or above the Reference Price: weight 1. Needs both sides (two_sided).
        return pro_rata(self._side(prog), 1.0, s, self.scored_depth(prog, side, book)[0])

    def qualifying_min(self, prog: Program, side: Side, book: Book) -> int:
        return qualifying(prog, sum(x.size for x in self._levels(side, book)))

    def s_star_closed_form(self, prog: Program, side: Side, book: Book, c: float) -> float | None:
        return None
