"""Polymarket US adapter. Public gateway only; no trade tape is public (see toml), so trades()
returns nothing and the runner marks fills as a lower bound."""

from __future__ import annotations

import math
from typing import Any

from bmm.scan.config import VenueCfg
from bmm.scan.http import GetOnlyClient
from bmm.scan.models import Book, Level, MarketMeta, Program, Side, Trade
from bmm.scan.venues.common import (
    classify,
    digest,
    geo_walk,
    iso_ns,
    jump_class,
    levels_sorted,
    pro_rata,
    qualifying,
    reference_quality,
)

GW = "https://gateway.polymarket.us"


class PmUs:
    name = "pm_us"

    def __init__(self, client: GetOnlyClient, cfg: VenueCfg) -> None:
        self.c, self.cfg = client, cfg
        self._meta: dict[str, MarketMeta] = {}

    def _side(self, prog: Program) -> float:
        return prog.R_usd_hr * float(self.cfg.raw.get("side_split", 0.5))

    async def programs(self) -> list[Program]:
        d = await self.c.get_json(self.cfg.raw.get("program_endpoint", f"{GW}/v1/incentives"))
        out: list[Program] = []
        for row in d["programs"][: int(self.cfg.raw.get("max_markets", 100))]:
            for tp in row.get("timePeriods", []):
                if tp.get("status") != "active":
                    continue
                start, end = iso_ns(tp.get("start")), iso_ns(tp.get("end"))
                hours = (end - start) / 3.6e12 if start and end and end > start else 24.0
                slug = row["marketSlug"]
                out.append(
                    Program(
                        venue=self.name, market_id=slug,
                        R_usd_hr=float(tp["rewardPool"]) / hours,
                        target_size=float(tp["targetSize"]) if tp.get("targetSize") else None,
                        max_spread=tp.get("maxSpread"),
                        venue_min=float(self.cfg.raw.get("venue_min", 1.0)),
                        discount=float(tp["discountFactor"]),
                        scoring="geometric_target",
                        two_sided=tp.get("maxSpread") is not None,
                        maker_fee=float(self.cfg.raw.get("maker_fee", 0.0)),
                        maker_rebate=float(self.cfg.raw.get("maker_rebate", 0.0)),
                        raw_hash=digest(tp),
                    )
                )  # fmt: skip
                cat = (
                    "sports"
                    if row.get("category") == "SPR"
                    else classify(slug, row.get("subcategory"))
                )
                self._meta[slug] = MarketMeta(
                    market_id=slug, title=slug, category=cat, expiry_ns=None,
                    settle_source=None, settle_ns=None,
                    reference_quality=reference_quality(cat),
                    in_game=tp.get("period") == "live",
                    jump_class=jump_class(cat, slug, row.get("subcategory")),
                )  # fmt: skip
                break
        return out

    async def meta(self, market_id: str) -> MarketMeta:
        return self._meta.get(market_id) or MarketMeta(
            market_id, market_id, classify(market_id), None, None, None, "none", False
        )

    async def book(self, market_id: str) -> Book:
        d: Any = await self.c.get_json(f"{GW}/v1/markets/{market_id}/book")
        md = d["marketData"]

        def px(rows: list[dict[str, Any]]) -> list[tuple[float, float]]:
            return [(float(r["px"]["value"]), float(r["qty"])) for r in rows]

        stats = md.get("stats") or {}
        st = stats.get("sharesTraded")
        return Book(
            levels_sorted(px(md.get("bids", [])), True),
            levels_sorted(px(md.get("offers", [])), False),
            shares_traded=float(st) if st not in (None, "") else None,
            opened_ns=iso_ns(stats.get("openSetTime")),
        )

    async def price_history(
        self, market_id: str
    ) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
        """(5-minute points for 1 day, 3-hour points for 1 week) of book-derived mids. Display
        prices: long = best ask, short = 1 - best bid, so mid = (long + 1 - short) / 2."""
        base = f"{GW}/v1/price-history"

        async def pts(interval: str, fidelity: str) -> list[tuple[int, float]]:
            d = await self.c.get_json(
                base, {"symbol": market_id, "fixedInterval": interval, "fidelity": fidelity}
            )
            return [
                (
                    int(h["timestamp"]) * 10**9,
                    (float(h["longPrice"]) + 1.0 - float(h["shortPrice"])) / 2,
                )
                for h in d.get("history", [])
            ]

        return await pts("INTERVAL_1D", "5"), await pts("INTERVAL_1W", "180")

    async def trades(self, market_id: str, since_ns: int) -> list[Trade]:
        return []  # no public tape (Time & Sales needs a key; WS needs auth)

    def _levels(self, side: Side, book: Book) -> list[Level]:
        return book.bids if side == "bid" else book.asks

    def scored_depth(self, prog: Program, side: Side, book: Book) -> tuple[float, float]:
        lv = self._levels(side, book)
        tick = float(self.cfg.raw.get("scoring_tick", 0.01))
        d = geo_walk(lv, prog.target_size, prog.discount or 1.0, tick)
        return d, (lv[0].size if lv else 0.0)  # raw = touch depth (fills happen there)

    def expected_reward(self, prog: Program, s: int, side: Side, book: Book) -> float:
        # We post at the best price: weight 1. Our size is not removed from the target walk,
        # which slightly overstates competitors' depth (conservative).
        return pro_rata(self._side(prog), 1.0, s, self.scored_depth(prog, side, book)[0])

    def qualifying_min(self, prog: Program, side: Side, book: Book) -> int:
        return qualifying(prog, sum(lv.size for lv in self._levels(side, book)))

    def s_star_closed_form(self, prog: Program, side: Side, book: Book, c: float) -> float | None:
        d = self.scored_depth(prog, side, book)[0]
        if d <= 0 or c <= 0:
            return None
        return math.sqrt(self._side(prog) * d / c) - d
