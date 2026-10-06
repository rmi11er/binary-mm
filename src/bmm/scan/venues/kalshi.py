"""Kalshi adapter. Incentives, orderbook and trades are public. A YES ask at a is a NO bid at 1-a,
so asks are derived from the NO bids."""

from __future__ import annotations

import bisect
import statistics
from typing import Any

from bmm.clock import now_ns
from bmm.scan.config import VenueCfg
from bmm.scan.http import GetOnlyClient
from bmm.scan.models import Book, Level, MarketMeta, Program, Side, Trade, VenueNotPublic
from bmm.scan.schedule import H_NS, make_schedule, reward_hours
from bmm.scan.venues.common import (
    DOMAIN_CATS,
    classify,
    digest,
    geo_walk,
    iso_ns,
    jump_class,
    levels_sorted,
    pro_rata,
    qualifying,
    reference_quality,
    ticker_category,
)

API = "https://external-api.kalshi.com/trade-api/v2"


def _grid(
    pts: list[tuple[int, float]], step: int, max_gap: int = 30 * 60 * 10**9
) -> list[tuple[int, float]]:
    """Forward-fill sparse candle mids onto a regular grid, at most `max_gap` past the last candle."""
    if not pts:
        return []
    tss = [t for t, _ in pts]
    out = []
    for g in range(-(-tss[0] // step), tss[-1] // step + 1):
        i = bisect.bisect_right(tss, g * step) - 1
        if g * step - tss[i] <= max_gap:
            out.append((g * step, pts[i][1]))
    return out


class Kalshi:
    name = "kalshi"
    _now = staticmethod(now_ns)  # injectable clock (tests)

    def __init__(self, client: GetOnlyClient, cfg: VenueCfg) -> None:
        self.c, self.cfg = client, cfg
        self._ev: dict[str, str] = {}
        self._series_of: dict[str, str] = {}
        self.history_source = "candles"

    def _side(self, prog: Program) -> float:
        return prog.R_usd_hr * float(self.cfg.raw.get("side_split", 0.5))

    async def _fetch_all(self, status: str) -> list[dict[str, Any]]:
        """Every program of a status, following next_cursor; stops on a page with no new ids."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        cur = ""
        for _ in range(int(self.cfg.raw.get("program_pages", 25))):
            params = {"status": status, "type": "liquidity", "limit": "200"}
            if cur:
                params["cursor"] = cur
            d = await self.c.get_json(
                self.cfg.raw.get("program_endpoint", f"{API}/incentive_programs"), params
            )
            new = [p for p in d.get("incentive_programs", []) if p["id"] not in seen]
            if not new:
                break
            seen.update(p["id"] for p in new)
            rows += new
            cur = d.get("next_cursor") or ""
            if not cur:
                break
        return rows

    async def programs(self) -> list[Program]:
        """Active programs, triaged for fetching: metals/FX/energy/rates/equity-index series first, then
        pool $/day descending, capped at max_markets (the per-host pacing budget).

        A series of recurring short windows (>= 3 windows within the next 24h, median <= 1h, e.g. the
        15-minute metals markets) is a STREAM: each program's schedule holds every window of the series,
        so $/day = the sum of the scheduled windows' pools while collateral stays the per-window
        requirement. Windows sharing a start time (several strikes) count once, at the median pool."""
        active = await self._fetch_all("active")
        ids = {p["id"] for p in active}
        upcoming = [p for p in await self._fetch_all("upcoming") if p["id"] not in ids]
        now = self._now()
        horizon = now + 24 * H_NS

        def parse(p: dict[str, Any]) -> dict[str, Any] | None:
            start, end = iso_ns(p["start_date"]), iso_ns(p["end_date"])
            if not start or not end or end <= start:
                return None
            return {"p": p, "ticker": p["market_ticker"], "series": p["market_ticker"].split("-")[0],
                    "start": start, "end": end, "pool": float(p["period_reward"]) / 1e4}  # fmt: skip

        parsed = [x for x in (parse(p) for p in active) if x]
        by_win: dict[str, dict[int, list[tuple[int, float]]]] = {}
        for x in [*parsed, *(y for y in (parse(p) for p in upcoming) if y)]:
            by_win.setdefault(x["series"], {}).setdefault(x["start"], []).append(
                (x["end"], x["pool"])
            )
        series_windows: dict[str, list[tuple[int, int, float]]] = {}
        for ser, ws in by_win.items():
            segs = []
            for st, lst in sorted(ws.items()):
                if st > horizon:
                    continue
                end = max(e for e, _ in lst)
                segs.append(
                    (st, end, statistics.median(pl_ for _, pl_ in lst) / ((end - st) / H_NS))
                )
            series_windows[ser] = segs
        out: list[Program] = []
        for x in parsed:
            p = x["p"]
            rate = x["pool"] / ((x["end"] - x["start"]) / H_NS)
            segs = [(x["start"], x["end"], rate)]
            n = 0
            sw = series_windows.get(x["series"], [])
            if len(sw) >= 3 and statistics.median(e - s for s, e, _ in sw) <= H_NS:
                segs, n = list(sw), len(sw)
            out.append(
                Program(
                    venue=self.name, market_id=x["ticker"], R_usd_hr=rate,
                    target_size=float(p["target_size_fp"]), max_spread=None,
                    venue_min=float(self.cfg.raw.get("venue_min", 1.0)),
                    discount=float(p["discount_factor_bps"]) / 1e4,
                    scoring="geometric_target", two_sided=True,
                    maker_fee=float(self.cfg.raw.get("maker_fee", 0.0)),
                    maker_rebate=float(self.cfg.raw.get("maker_rebate", 0.0)),
                    raw_hash=digest(p), start_ns=x["start"], end_ns=x["end"], pool_usd=x["pool"],
                    schedule=make_schedule(segs), stream_windows=n,
                )
            )  # fmt: skip

        def key(prog: Program) -> tuple[int, float, str]:
            dom = ticker_category(prog.market_id) in DOMAIN_CATS
            return (0 if dom else 1, -prog.R_usd_hr * reward_hours(prog, now), prog.market_id)

        out.sort(key=key)
        return out[: int(self.cfg.raw.get("max_markets", 100))]

    async def meta(self, market_id: str) -> MarketMeta:
        m: dict[str, Any] = (await self.c.get_json(f"{API}/markets/{market_id}"))["market"]
        cat = ticker_category(market_id) or classify(m.get("title"), market_id)
        exp = iso_ns(m.get("expected_expiration_time") or m.get("close_time"))
        return MarketMeta(
            market_id=market_id, title=m.get("title", market_id), category=cat, expiry_ns=exp,
            settle_source=(m.get("rules_primary") or "")[:200] or None, settle_ns=exp,
            reference_quality=reference_quality(cat), in_game=False,
            jump_class=jump_class(cat, m.get("title"), market_id, m.get("rules_primary")),
        )  # fmt: skip

    async def book(self, market_id: str) -> Book:
        d = (await self.c.get_json(f"{API}/markets/{market_id}/orderbook"))["orderbook_fp"]
        yes = [(float(p), float(q)) for p, q in d.get("yes_dollars", [])]
        no = [(1.0 - float(p), float(q)) for p, q in d.get("no_dollars", [])]
        return Book(levels_sorted(yes, True), levels_sorted(no, False))

    async def trades(self, market_id: str, since_ns: int) -> list[Trade]:
        return (await self.trades_resume(market_id, since_ns, None))[0]

    async def trades_resume(
        self, market_id: str, since_ns: int, cursor: str | None
    ) -> tuple[list[Trade], str]:
        """Pages newest -> oldest. Returns (trades, cursor): a non-empty cursor means the page cap
        stopped us with older pages left, so a later scan can resume there (incremental backfill)."""
        out: dict[str, Trade] = {}
        cur = cursor or ""
        for _ in range(int(self.cfg.raw.get("trade_pages", 6))):
            params = {"ticker": market_id, "min_ts": str(since_ns // 10**9), "limit": "1000"}
            if cur:
                params["cursor"] = cur
            d = await self.c.get_json(f"{API}/markets/trades", params)
            for t in d.get("trades", []):
                # taker_outcome_side "yes" = the taker bought YES (lifted the ask) -> "buy".
                out[t["trade_id"]] = Trade(
                    iso_ns(t["created_time"]) or 0, float(t["yes_price_dollars"]),
                    float(t["count_fp"]), "buy" if t.get("taker_outcome_side") == "yes" else "sell",
                )  # fmt: skip
            cur = d.get("cursor") or ""
            if not cur or not d.get("trades"):
                cur = ""
                break
        return sorted(out.values(), key=lambda t: t.ts_ns), cur

    async def _series(self, market_id: str) -> str:
        ev = self._ev.get(market_id)
        if ev is None:
            m = (await self.c.get_json(f"{API}/markets/{market_id}"))["market"]
            ev = self._ev[market_id] = m["event_ticker"]
        if ev not in self._series_of:
            e = (await self.c.get_json(f"{API}/events/{ev}"))["event"]
            self._series_of[ev] = e.get("series_ticker") or ev.split("-")[0]
        return self._series_of[ev]

    async def price_history(
        self, market_id: str
    ) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
        """Candlestick yes_bid/yes_ask close mid: 1-minute candles for 24h (forward-filled to a
        5-minute grid) and 60-minute candles for 7d. Endpoint: GET /series/{series}/markets/
        {ticker}/candlesticks (https://docs.kalshi.com/api-reference/market/get-market-candlesticks.md)."""
        series = await self._series(market_id)
        now = now_ns() // 10**9

        async def candles(start: int, interval: int) -> list[tuple[int, float]]:
            d = await self.c.get_json(
                f"{API}/series/{series}/markets/{market_id}/candlesticks",
                {"start_ts": str(start), "end_ts": str(now), "period_interval": str(interval)},
            )
            pts = []
            for c in d.get("candlesticks", []):
                b, a = (
                    (c.get("yes_bid") or {}).get("close_dollars"),
                    (c.get("yes_ask") or {}).get("close_dollars"),
                )
                if b is not None and a is not None and float(b) > 0 and float(a) < 1:
                    pts.append((int(c["end_period_ts"]) * 10**9, (float(b) + float(a)) / 2))
            return sorted(pts)

        fine_raw = await candles(now - 86400, 1)
        coarse = await candles(now - 7 * 86400, 60)
        if not fine_raw and not coarse:
            raise VenueNotPublic(f"kalshi {market_id}: no candle bid/ask history")
        self.history_source = "candles"
        return _grid(fine_raw, 5 * 60 * 10**9), coarse

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
