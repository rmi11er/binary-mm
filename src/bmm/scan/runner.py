"""Snapshot loop with two cadences (spec 60).

Hourly ("full") tier: program terms + universe, meta, books and trades for every market, plus a
7-day backfill for markets seen for the first time. 5-minute ("fast") tier: books and trades only,
for the top candidates from the last ranking, reusing stored terms and meta. Venues and markets
run concurrently; the HTTP client's per-host pacing keeps the request rate polite.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from bmm.clock import now_ns
from bmm.scan import config as cfgmod
from bmm.scan import store
from bmm.scan.config import VenueCfg
from bmm.scan.http import GetOnlyClient
from bmm.scan.metrics import (
    H_NS,
    eval_coll,
    exclusion,
    fills_contracts_hr,
    fills_from_shares,
    markout_30m,
    mean_abs_move,
    resample_trades,
    side_of,
    tail_move_1h,
)
from bmm.scan.models import (
    Book,
    MarketMeta,
    Program,
    Side,
    Trade,
    VenueAdapter,
    VenueNotPublic,
)
from bmm.scan.report import refresh_ranking, top_candidates

log = logging.getLogger("bmm.scan")

DAY_NS = 24 * H_NS
WINDOW_NS = DAY_NS  # fill-rate window
BACKFILL_NS = 7 * DAY_NS
FULL_PERIOD_NS = H_NS
FAST_TOP_N = 40
FAST_BUDGET_S = 90.0
CONCURRENCY = 4
BOOK_LEVELS = 20
MARKOUT_NS = 30 * 60 * 10**9
SIDE_COLL_DEFAULT = {"bid": "p", "ask": "1-p"}
EMPTY_MIDS = pl.DataFrame(
    {"ts_ns": pl.Series([], dtype=pl.Int64), "mid": pl.Series([], dtype=pl.Float64)}
)
TRADE_COLS = ["ts_ns", "price", "size", "aggressor"]
WEAK_SOURCES = ("trade_buysell", "trade_derived")  # trade-derived mids (old and new labels)


def due_full(last_full: int | None, now: int, period_ns: int = FULL_PERIOD_NS) -> bool:
    return last_full is None or now - last_full >= period_ns


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1e9, tz=UTC).strftime("%Y-%m-%d")


def _history(table: str, now: int, root: Path, days: int = 2) -> pl.DataFrame:
    frames = [
        f
        for d in sorted({_day(now - k * DAY_NS) for k in range(days)})
        if not (f := store.read(table, d, root)).is_empty()
    ]
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


def last_full_ns(root: Path, now: int) -> int | None:
    p = _history("programs", now, root)
    return None if p.is_empty() else int(p["ts_ns"].max())  # type: ignore[arg-type]


def _sel(df: pl.DataFrame, venue: str, mkt: str) -> pl.DataFrame:
    if df.is_empty():
        return df
    return df.filter((pl.col("venue") == venue) & (pl.col("market_id") == mkt))


def _book_row(ts: int, venue: str, mid_id: str, book: Book) -> dict[str, Any]:
    b, a = book.bids[:BOOK_LEVELS], book.asks[:BOOK_LEVELS]
    return {
        "ts_ns": ts, "venue": venue, "market_id": mid_id,
        "bid_prices": [x.price for x in b], "bid_sizes": [x.size for x in b],
        "bid_holder": [x.top_holder_share for x in b],
        "ask_prices": [x.price for x in a], "ask_sizes": [x.size for x in a],
        "ask_holder": [x.top_holder_share for x in a],
    }  # fmt: skip


def _mids(df: pl.DataFrame) -> pl.DataFrame:
    return EMPTY_MIDS if df.is_empty() else df.select("ts_ns", "mid").drop_nulls().unique("ts_ns")


@dataclass
class Ctx:
    snaps: pl.DataFrame  # 2 days: organic mids, previous shares_traded, stored meta
    trades: pl.DataFrame  # 8 days
    proxy: pl.DataFrame  # 8 days of backfilled mid series
    progs: pl.DataFrame  # 2 days of stored terms (fast tier)
    cursors: pl.DataFrame = field(default_factory=pl.DataFrame)  # Kalshi backfill resume points


def _meta_from_row(r: dict[str, Any]) -> MarketMeta:
    return MarketMeta(
        r["market_id"], r.get("title") or r["market_id"], r.get("category") or "other",
        r.get("expiry_ns"), r.get("settle_source"), r.get("settle_ns"),
        r.get("reference_quality") or "none", r.get("excluded") == "live in-game",
        r.get("jump_class") or "normal",
    )  # fmt: skip


async def _market_rows(
    a: VenueAdapter,
    prog: Program,
    meta: MarketMeta | None,
    now: int,
    cfg: VenueCfg | None,
    ctx: Ctx,
    out: dict[str, list[dict[str, Any]]],
    write_terms: bool,
) -> None:
    mkt = prog.market_id
    meta = meta or await a.meta(mkt)
    book = await a.book(mkt)
    old = _sel(ctx.trades, a.name, mkt)
    last = int(old["ts_ns"].max()) if not old.is_empty() else 0  # type: ignore[arg-type]

    # --- trades. Venues with a cursor-aware tape resume their 7-day backfill across scans. ---
    resume = getattr(a, "trades_resume", None)
    if callable(resume):
        cur_rows = _sel(ctx.cursors, a.name, mkt)
        latest = cur_rows.sort("ts_ns").tail(1) if not cur_rows.is_empty() else None
        stored_cur = str(latest["cursor"][0] or "") if latest is not None else ""
        stored_min = now - BACKFILL_NS
        if latest is not None and "min_ts" in latest.columns and latest["min_ts"][0] is not None:
            stored_min = int(latest["min_ts"][0])
        cursor_min = stored_min
        if not last:  # first sight: newest pages of the 7d window
            cursor_min = now - BACKFILL_NS
            new, cursor_out = await resume(mkt, cursor_min, None)
        else:
            new, gap = await resume(mkt, last + 1, None)  # what is new since the last scan
            for _ in range(20):  # never leave a gap between scans: page until it is closed
                if not gap:
                    break
                more, gap = await resume(mkt, last + 1, gap)
                new = more + new
            cursor_out = stored_cur
            if (
                stored_cur and write_terms
            ):  # older pages: hourly tier, with the min_ts that issued the cursor
                if stored_min >= now - BACKFILL_NS - DAY_NS:
                    older, cursor_out = await resume(mkt, stored_min, stored_cur)
                    new = older + new
                else:
                    cursor_out = ""  # backfill stalled for over a day: give up on the old pages
        out["cursors"].append(
            {
                "venue": a.name,
                "market_id": mkt,
                "cursor": cursor_out or "",
                "min_ts": cursor_min,
                "ts_ns": now,
            }
        )
    else:
        new = await a.trades(mkt, max(now - BACKFILL_NS, last + 1) if last else now - BACKFILL_NS)
    new_rows = [
        {"ts_ns": t.ts_ns, "venue": a.name, "market_id": mkt, "price": t.price, "size": t.size,
         "aggressor": t.aggressor}
        for t in new
    ]  # fmt: skip
    out["trades"] += new_rows
    parts = [df.select(TRADE_COLS) for df in (old, pl.DataFrame(new_rows)) if not df.is_empty()]
    tr_all = (
        pl.concat(parts, how="diagonal_relaxed")
        .unique()
        .filter(pl.col("ts_ns") >= now - BACKFILL_NS)
        if parts
        else pl.DataFrame({c: [] for c in TRADE_COLS})
    )
    trade_objs = (
        [Trade(*r) for r in tr_all.select(TRADE_COLS).iter_rows()] if not tr_all.is_empty() else []
    )
    tr_24 = [t for t in trade_objs if t.ts_ns >= now - WINDOW_NS]

    # --- proxy mid series (stored, else backfilled) and where it came from ---
    stored = _sel(ctx.proxy, a.name, mkt)
    if not stored.is_empty():
        source = str(stored["source"][0]) if "source" in stored.columns else "trade_buysell"
        fine = [
            (int(t), float(m))
            for t, m in stored.filter(pl.col("kind") == "fine").select("ts_ns", "mid").iter_rows()
        ]
        coarse = [
            (int(t), float(m))
            for t, m in stored.filter(pl.col("kind") == "coarse").select("ts_ns", "mid").iter_rows()
        ]
    else:
        fine, coarse, source = [], [], "trade_buysell"
        hist_fn = getattr(a, "price_history", None)
        if callable(hist_fn):
            try:
                fine, coarse = await hist_fn(mkt)
                source = getattr(a, "history_source", "price_history")
            except (VenueNotPublic, KeyError, ValueError) as e:
                log.warning("price history %s/%s unavailable: %r", a.name, mkt, e)
        if not fine:  # weak fallback: average of last buy / last sell per bucket
            fine, source = resample_trades(trade_objs), "trade_buysell"
        out["proxy_mids"] += [
            {"venue": a.name, "market_id": mkt, "ts_ns": t, "mid": m, "kind": k, "source": source}
            for k, ser in (("fine", fine), ("coarse", coarse))
            for t, m in ser
        ]  # fmt: skip

    bid = book.bids[0].price if book.bids else None
    ask = book.asks[0].price if book.asks else None
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    org = _sel(ctx.snaps, a.name, mkt)
    org_mids = _mids(org)
    if mid is not None:
        org_mids = pl.concat(
            [org_mids, pl.DataFrame({"ts_ns": [now], "mid": [mid]}, schema=EMPTY_MIDS.schema)]
        )
    proxy_df = pl.DataFrame(
        {"ts_ns": [t for t, _ in fine], "mid": [m for _, m in fine]}, schema=EMPTY_MIDS.schema
    )
    # The proxied prior may only use fills BEFORE our first organic snapshot, so it is independent
    # of the organic estimate that later replaces it.
    first_org = int(org["ts_ns"].min()) if not org.is_empty() else now + 1  # type: ignore[arg-type]
    tr_pre = tr_all.filter(pl.col("ts_ns") < first_org) if not tr_all.is_empty() else tr_all
    vol30, _ = mean_abs_move(fine, MARKOUT_NS)
    p99 = tail_move_1h(fine, coarse, fine_obs=len(trade_objs) if source in WEAK_SOURCES else None)

    # --- fills: tape where there is one, else estimated from sharesTraded ---
    no_tape = bool(cfg and cfg.raw.get("trades_aggressor") == "none")
    use_shares = book.shares_traded is not None and (no_tape or not trade_objs)
    total_est: float | None = None
    if use_shares:
        prev = None
        if not org.is_empty() and "shares_traded" in org.columns:
            o = org.filter(
                (pl.col("ts_ns") >= now - WINDOW_NS) & pl.col("shares_traded").is_not_null()
            ).sort("ts_ns")
            if not o.is_empty():
                prev = (int(o["ts_ns"][0]), float(o["shares_traded"][0]))
        total_est = fills_from_shares(float(book.shares_traded or 0.0), now, book.opened_ns, prev)

    reason = exclusion(meta, book, now)
    ref_fair: float | None = None  # only venue_ref markets, only if the adapter can supply one
    ref_fn = getattr(a, "reference_fair", None)
    if meta.reference_quality == "venue_ref" and callable(ref_fn):
        try:
            ref_fair = await ref_fn(mkt)
        except (VenueNotPublic, KeyError, ValueError) as e:
            log.warning("reference %s/%s unavailable: %r", a.name, mkt, e)
    bid_size = book.bids[0].size if book.bids else None
    ask_size = book.asks[0].size if book.asks else None
    pq = mid * (1 - mid) if mid is not None else 0.25  # fee/rebate coefficients scale with p(1-p)
    split = float(cfg.raw.get("side_split", 0.5)) if cfg else 0.5
    verified = bool(cfg.raw.get("coll_verified", False)) if cfg else False
    if write_terms:
        out["programs"].append({"ts_ns": now, **dataclasses.asdict(prog)})
    out["books"].append(_book_row(now, a.name, mkt, book))
    for side in ("bid", "ask"):
        s: Side = side  # type: ignore[assignment]
        D, raw = a.scored_depth(prog, s, book)
        px = (bid if s == "bid" else ask) or mid or 0.5
        expr = cfg.raw.get(f"coll_{side}") if cfg else None
        if (
            expr is None
            or str(expr).strip().lower() in {"", "unknown"}
            or str(expr).strip().startswith("[")
        ):
            expr = SIDE_COLL_DEFAULT[side]
        m_org, n_org = markout_30m(tr_all, org_mids, s)
        m_prox, n_prox = markout_30m(tr_pre, proxy_df, s)
        touch = (book.bids if s == "bid" else book.asks)[:1]
        fills = (
            (total_est or 0.0) / 2
            if use_shares
            else fills_contracts_hr(tr_24, s, org_mids, WINDOW_NS)
        )
        out["snapshots"].append(
            {
                "ts_ns": now, "venue": a.name, "market_id": mkt, "side": side, "title": meta.title,
                "category": meta.category, "jump_class": meta.jump_class, "expiry_ns": meta.expiry_ns,
                "R_side": prog.R_usd_hr * split, "target_size": prog.target_size,
                "max_spread": prog.max_spread, "venue_min": prog.venue_min,
                "discount": prog.discount, "scoring": prog.scoring, "two_sided": prog.two_sided,
                "D_side": D, "depth_raw_side": raw, "bid": bid, "ask": ask, "mid": mid,
                "spread": (ask - bid) if bid is not None and ask is not None else None,
                "top_holder_share": touch[0].top_holder_share if touch else None,
                "fills_side_hr": fills, "fills_proxied": use_shares,
                "fills_shares_hr": total_est if use_shares else None,
                "fills_lower_bound": (use_shares and total_est is None) or (no_tape and not use_shares),
                "shares_traded": book.shares_traded,
                "markout_30m_side": m_org, "n_markout_side": n_org,
                "markout_proxied_side": m_prox, "n_markout_proxied_side": n_prox,
                "mid_source": source, "weak_proxy": source in WEAK_SOURCES,
                "tier": "full" if write_terms else "fast", "bid_size": bid_size, "ask_size": ask_size,
                "ref_fair": ref_fair, "taker_fee_coeff": cfg.raw.get("taker_fee") if cfg and cfg.raw.get("taker_fee_verified") else None,
                "vol30_proxy": vol30, "jump_p99_1h": p99,
                "n_trades_side": sum(1 for t in tr_24 if side_of(t, mid) == s),
                "coll_side": eval_coll(str(expr), px), "coll_verified": verified,
                "maker_fee": prog.maker_fee * pq, "maker_rebate": prog.maker_rebate * pq,
                "settle_source": meta.settle_source, "settle_ns": meta.settle_ns,
                "reference_quality": meta.reference_quality, "excluded": reason,
            }
        )  # fmt: skip


def _program(row: dict[str, Any]) -> Program:
    return Program(**{k: row[k] for k in Program.__dataclass_fields__ if k in row})


async def snapshot_once(
    adapters: list[VenueAdapter],
    now: int,
    cfgs: dict[str, VenueCfg] | None = None,
    root: Path = store.DEFAULT_ROOT,
    only: set[tuple[str, str]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """One pass. `only` = fast tier: just those (venue, market) pairs, from stored terms and meta."""
    out: dict[str, list[dict[str, Any]]] = {
        k: [] for k in ("snapshots", "programs", "trades", "books", "proxy_mids", "cursors")
    }
    ctx = Ctx(
        _history("snapshots", now, root),
        _history("trades", now, root, days=8),
        _history("proxy_mids", now, root, days=8),
        _history("programs", now, root) if only is not None else pl.DataFrame(),
    )
    ctx.cursors = _history("cursors", now, root, days=8)
    if ctx.trades.is_empty() is False:
        ctx.trades = ctx.trades.unique(subset=["venue", "market_id", "ts_ns", "price", "size"])
    stored_meta: dict[tuple[str, str], MarketMeta] = {}
    stored_prog: dict[tuple[str, str], Program] = {}
    if only is not None and not ctx.snaps.is_empty() and not ctx.progs.is_empty():
        for r in (
            ctx.snaps.sort("ts_ns").group_by(["venue", "market_id"]).last().iter_rows(named=True)
        ):
            stored_meta[(r["venue"], r["market_id"])] = _meta_from_row(r)
        for r in (
            ctx.progs.sort("ts_ns").group_by(["venue", "market_id"]).last().iter_rows(named=True)
        ):
            stored_prog[(r["venue"], r["market_id"])] = _program(r)

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(a: VenueAdapter, prog: Program, meta: MarketMeta | None) -> None:
        async with sem:
            try:
                await _market_rows(
                    a, prog, meta, now, (cfgs or {}).get(a.name), ctx, out, only is None
                )
            except Exception as e:  # noqa: BLE001 - one bad market must not kill the scan; logged
                log.warning("market %s/%s skipped: %r", a.name, prog.market_id, e)

    async def venue(a: VenueAdapter) -> None:
        if only is None:
            try:
                progs = await a.programs()
            except VenueNotPublic as e:
                log.warning("venue %s skipped: %s", a.name, e)
                return
            items = [(p, None) for p in progs]
        else:
            items = [
                (stored_prog[k], stored_meta.get(k))
                for k in sorted(only)
                if k[0] == a.name and k in stored_prog
            ]
        await asyncio.gather(*(one(a, p, m) for p, m in items))

    await asyncio.gather(*(venue(a) for a in adapters))
    return out


def _flush(tables: dict[str, list[dict[str, Any]]], ts: int, root: Path) -> None:
    for tname, rows in tables.items():
        if rows:
            store.write(tname, rows, ts, root)


async def _loop(adapters: list[VenueAdapter], conf: cfgmod.ScanConfig, once: bool, interval: int,
                force_full: bool, top_n: int, root: Path) -> int:  # fmt: skip
    ads = {a.name: a for a in adapters}
    while True:
        t0 = time.monotonic()
        ts = now_ns()
        only: set[tuple[str, str]] | None = None
        if not (force_full or due_full(last_full_ns(root, ts), ts)):
            only = top_candidates(root, top_n, _day(ts)) or None
        tables = await snapshot_once(adapters, ts, conf.venue, root, only=only)
        _flush(tables, ts, root)
        if only is None:
            refresh_ranking(_day(ts), root, ads, conf)
        dt = time.monotonic() - t0
        tier = "full" if only is None else f"fast(top {len(only)})"
        print(
            f"scan {ts} [{tier}] {dt:.0f}s: "
            + ", ".join(f"{k}={len(v)}" for k, v in tables.items()),
            flush=True,
        )
        if only is not None and dt > FAST_BUDGET_S:
            log.warning(
                "fast tier took %.0fs, over the %.0fs budget; lower --top", dt, FAST_BUDGET_S
            )
        if once:
            return 0
        await asyncio.sleep(max(0.0, interval - dt))


def run(venues: list[str], once: bool, interval: int, force_full: bool = False,
        top_n: int = FAST_TOP_N, root: Path = store.DEFAULT_ROOT) -> int:  # fmt: skip
    from bmm.scan.venues import ADAPTERS

    conf = cfgmod.load()
    client = GetOnlyClient(set(conf.hosts))
    adapters: list[VenueAdapter] = []
    for name in venues:
        cfg = conf.venue.get(name)
        if cfg is None or not cfg.enabled:
            log.warning("venue %s skipped: %s", name, cfg.skip_reason if cfg else "not configured")
            print(f"skip {name}: {cfg.skip_reason if cfg else 'not configured'}")
            continue
        adapters.append(ADAPTERS[name](client, cfg))
    if not adapters:
        print("no enabled venues", flush=True)
        return 1
    return asyncio.run(_loop(adapters, conf, once, interval, force_full, top_n, root))
