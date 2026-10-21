"""Polymarket US adapter. Public gateway only; no trade tape is public (see toml), so trades()
returns nothing and the runner marks fills as a lower bound."""

from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Any

from bmm.clock import now_ns
from bmm.scan.config import VenueCfg
from bmm.scan.http import GetOnlyClient
from bmm.scan.models import Book, Level, MarketMeta, Program, Side, Trade
from bmm.scan.schedule import make_schedule, reward_hours
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
log = logging.getLogger("bmm.scan")


class PmUs:
    name = "pm_us"

    def __init__(self, client: GetOnlyClient, cfg: VenueCfg) -> None:
        self.c, self.cfg = client, cfg
        self._meta: dict[str, MarketMeta] = {}

    def _side(self, prog: Program) -> float:
        return prog.R_usd_hr * float(self.cfg.raw.get("side_split", 0.5))

    _now = staticmethod(now_ns)  # injectable clock (tests)

    async def all_programs(self) -> list[Program]:
        """Every market with an active period, paged by nextPageToken (query params are snake_case:
        camelCase ones are silently ignored). Futures (period daily_event) pools are pro-rated.

        POOL SCOPE [?]: the docs define the futures period as "a per-day pool on an event without a fixed
        start" (https://docs.polymarket.us/incentives/liquidity.md, FAQ "What do the time periods mean?"),
        and the API repeats the same rewardPool on every instrument row of an event (465 of 465 futures
        events in the 2026-10-06 data, up to 115 instruments sharing one value). So the pool is divided
        equally across the event's instruments here. The docs do not say how the exchange divides it, so
        equal split is an assumption. Game periods (day_of / live) are NOT pro-rated: same doubt, no evidence."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        tok = ""
        for _ in range(int(self.cfg.raw.get("program_pages", 200))):
            params = {"page_size": "500"}
            if tok:
                params["page_token"] = tok
            d = await self.c.get_json(
                self.cfg.raw.get("program_endpoint", f"{GW}/v1/incentives"), params
            )
            new = [r for r in d.get("programs", []) if r["marketSlug"] not in seen]
            if not new:
                tok = ""
                break
            seen.update(r["marketSlug"] for r in new)
            rows += new
            tok = d.get("nextPageToken") or ""
            if not tok:
                break
        if tok:
            log.warning(
                "pm_us incentives: page cap reached with more pages pending (%d markets read)",
                len(rows),
            )
        cur_rows: list[
            tuple[dict[str, Any], dict[str, Any], list[tuple[Any, ...]], str, str, str]
        ] = []
        counts: Counter[tuple[str, str]] = Counter()
        for row in rows:
            segs: list[tuple[int | None, int | None, float, bool]] = []
            cur: dict[str, Any] | None = None
            for tp in row.get("timePeriods", []):
                if tp.get("status") not in ("active", "upcoming", "scheduled", "pending"):
                    continue
                s0, e0 = iso_ns(tp.get("start")), iso_ns(tp.get("end"))
                hrs = (
                    (e0 - s0) / 3.6e12 if s0 and e0 and e0 > s0 else 24.0
                )  # open end: a daily pool
                segs.append(
                    (s0, e0, float(tp["rewardPool"]) / hrs, tp.get("period") == "daily_event")
                )
                if cur is None and tp.get("status") == "active":
                    cur = tp
            if cur is None:
                continue
            pid, per = cur.get("programId", ""), cur.get("period", "")
            ek = row["marketSlug"].rsplit("-", 1)[0] if per == "daily_event" else row["marketSlug"]
            if per == "daily_event":
                counts[(pid, ek)] += 1
            cur_rows.append((row, cur, segs, pid, per, ek))
        out: list[Program] = []
        for row, cur, segs, pid, per, ek in cur_rows:
            n = counts[(pid, ek)] if per == "daily_event" else 1
            slug = row["marketSlug"]
            start, end = iso_ns(cur.get("start")), iso_ns(cur.get("end"))
            hours = (end - start) / 3.6e12 if start and end and end > start else 24.0
            pool = float(cur["rewardPool"])
            out.append(
                Program(
                    venue=self.name, market_id=slug,
                    R_usd_hr=pool / hours / n,
                    target_size=float(cur["targetSize"]) if cur.get("targetSize") else None,
                    max_spread=cur.get("maxSpread"),
                    venue_min=float(self.cfg.raw.get("venue_min", 1.0)),
                    discount=float(cur["discountFactor"]),
                    scoring="geometric_target",
                    two_sided=cur.get("maxSpread") is not None,
                    maker_fee=float(self.cfg.raw.get("maker_fee", 0.0)),
                    maker_rebate=float(self.cfg.raw.get("maker_rebate", 0.0)),
                    raw_hash=digest(cur),
                    start_ns=start, end_ns=end, pool_usd=pool / n,
                    schedule=make_schedule([(a_, b_, r / n if ev else r) for a_, b_, r, ev in segs]),
                    program_id=pid, period=per, event_key=ek if per == "daily_event" else "",
                    event_n=n, pool_event_usd=pool,
                )
            )  # fmt: skip
            cat = (
                "sports" if row.get("category") == "SPR" else classify(slug, row.get("subcategory"))
            )
            self._meta[slug] = MarketMeta(
                market_id=slug, title=slug, category=cat, expiry_ns=None,
                settle_source=None, settle_ns=None,
                reference_quality=reference_quality(cat),
                in_game=cur.get("period") == "live",
                jump_class=jump_class(cat, slug, row.get("subcategory")),
            )  # fmt: skip
        return out

    async def programs(self) -> list[Program]:
        """All programs, triaged for fetching by pro-rated pool $/day, capped at max_markets."""
        progs = await self.all_programs()
        now = self._now()
        progs.sort(key=lambda p: (-(p.R_usd_hr * reward_hours(p, now)), p.market_id))
        return progs[: int(self.cfg.raw.get("max_markets", 100))]

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
