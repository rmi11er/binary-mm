"""Snapshot loop: one pass over every enabled venue, then append parquet tables."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
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
    markout_30m,
    side_of,
)
from bmm.scan.models import Book, Level, Program, Side, Trade, VenueAdapter, VenueNotPublic

log = logging.getLogger("bmm.scan")

WINDOW_NS = 24 * H_NS  # fill-rate window
BOOK_LEVELS = 20
DAY_NS = 24 * H_NS
SIDE_COLL_DEFAULT = {"bid": "p", "ask": "1-p"}


def _history(table: str, now: int, root: Path) -> pl.DataFrame:
    """Rows from today and yesterday (UTC) so markouts can span midnight."""
    from datetime import UTC, datetime

    days = {
        datetime.fromtimestamp((now - k * DAY_NS) / 1e9, tz=UTC).strftime("%Y-%m-%d")
        for k in (0, 1)
    }
    frames = [f for d in sorted(days) if not (f := store.read(table, d, root)).is_empty()]
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


def _levels(lv: list[Level]) -> list[Level]:
    return lv[:BOOK_LEVELS]


def _book_row(ts: int, venue: str, mid_id: str, book: Book) -> dict[str, Any]:
    b, a = _levels(book.bids), _levels(book.asks)
    return {
        "ts_ns": ts, "venue": venue, "market_id": mid_id,
        "bid_prices": [x.price for x in b], "bid_sizes": [x.size for x in b],
        "bid_holder": [x.top_holder_share for x in b],
        "ask_prices": [x.price for x in a], "ask_sizes": [x.size for x in a],
        "ask_holder": [x.top_holder_share for x in a],
    }  # fmt: skip


async def _market_rows(
    a: VenueAdapter,
    prog: Program,
    now: int,
    cfg: VenueCfg | None,
    snaps: pl.DataFrame,
    trades_hist: pl.DataFrame,
    out: dict[str, list[dict[str, Any]]],
) -> None:
    mkt = prog.market_id
    meta = await a.meta(mkt)
    book = await a.book(mkt)
    old = (
        trades_hist.filter((pl.col("venue") == a.name) & (pl.col("market_id") == mkt))
        if not trades_hist.is_empty()
        else trades_hist
    )
    last = int(old["ts_ns"].max()) if not old.is_empty() else 0  # type: ignore[arg-type]
    new: list[Trade] = await a.trades(mkt, max(now - WINDOW_NS, last + 1))
    new_rows = [
        {"ts_ns": t.ts_ns, "venue": a.name, "market_id": mkt, "price": t.price, "size": t.size,
         "aggressor": t.aggressor}
        for t in new
    ]  # fmt: skip
    out["trades"] += new_rows
    cols = ["ts_ns", "price", "size", "aggressor"]
    parts = [df.select(cols) for df in (old, pl.DataFrame(new_rows)) if not df.is_empty()]
    tr = (
        pl.concat(parts, how="diagonal_relaxed").filter(pl.col("ts_ns") >= now - WINDOW_NS)
        if parts
        else pl.DataFrame({c: [] for c in cols})
    )
    bid = book.bids[0].price if book.bids else None
    ask = book.asks[0].price if book.asks else None
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    hist_mid = (
        snaps.filter((pl.col("venue") == a.name) & (pl.col("market_id") == mkt))
        .select("ts_ns", "mid")
        .unique()
        if not snaps.is_empty()
        else pl.DataFrame({"ts_ns": [], "mid": []})
    )
    mids = (
        hist_mid.vstack(pl.DataFrame({"ts_ns": [now], "mid": [mid]}, schema=hist_mid.schema))
        if mid is not None
        else hist_mid
    )
    trade_objs = [Trade(*r) for r in tr.select(cols).iter_rows()] if not tr.is_empty() else []
    reason = exclusion(meta, book, now)
    split = float(cfg.raw.get("side_split", 0.5)) if cfg else 0.5
    verified = bool(cfg.raw.get("coll_verified", False)) if cfg else False
    out["programs"].append({"ts_ns": now, **dataclasses.asdict(prog)})
    out["books"].append(_book_row(now, a.name, mkt, book))
    for side in ("bid", "ask"):
        s: Side = side  # type: ignore[assignment]
        D, raw = a.scored_depth(prog, s, book)
        px = (bid if s == "bid" else ask) or mid or 0.5
        expr = cfg.raw.get(f"coll_{side}") if cfg else None
        if expr is None or str(expr).strip().startswith("["):
            expr = SIDE_COLL_DEFAULT[side]
        m, n_mo = markout_30m(tr, mids, s)
        n_side = sum(1 for t in trade_objs if side_of(t, mid) == s)
        touch = (book.bids if s == "bid" else book.asks)[:1]
        out["snapshots"].append(
            {
                "ts_ns": now, "venue": a.name, "market_id": mkt, "side": side, "title": meta.title,
                "category": meta.category, "expiry_ns": meta.expiry_ns,
                "R_side": prog.R_usd_hr * split, "target_size": prog.target_size,
                "max_spread": prog.max_spread, "venue_min": prog.venue_min,
                "discount": prog.discount, "scoring": prog.scoring, "two_sided": prog.two_sided,
                "D_side": D, "depth_raw_side": raw, "bid": bid, "ask": ask, "mid": mid,
                "spread": (ask - bid) if bid is not None and ask is not None else None,
                "top_holder_share": touch[0].top_holder_share if touch else None,
                "fills_side_hr": fills_contracts_hr(trade_objs, s, mids, WINDOW_NS),
                "markout_30m_side": m, "n_trades_side": n_side, "n_markout_side": n_mo,
                "coll_side": eval_coll(str(expr), px), "coll_verified": verified,
                "maker_fee": prog.maker_fee, "maker_rebate": prog.maker_rebate,
                "settle_source": meta.settle_source, "settle_ns": meta.settle_ns,
                "reference_quality": meta.reference_quality, "excluded": reason,
            }
        )  # fmt: skip


async def snapshot_once(
    adapters: list[VenueAdapter],
    now: int,
    cfgs: dict[str, VenueCfg] | None = None,
    root: Path = store.DEFAULT_ROOT,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {
        "snapshots": [],
        "programs": [],
        "trades": [],
        "books": [],
    }
    snaps, trades_hist = _history("snapshots", now, root), _history("trades", now, root)
    for a in adapters:
        try:
            progs = await a.programs()
        except VenueNotPublic as e:
            log.warning("venue %s skipped: %s", a.name, e)
            continue
        for prog in progs:
            try:
                await _market_rows(
                    a,
                    prog,
                    now,
                    (cfgs or {}).get(a.name),
                    snaps,
                    trades_hist,
                    out,
                )
            except VenueNotPublic as e:
                log.warning("market %s/%s skipped: %s", a.name, prog.market_id, e)
    return out


def run(venues: list[str], once: bool, interval: int) -> int:
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
    while True:
        ts = now_ns()
        tables = asyncio.run(snapshot_once(adapters, ts, conf.venue))
        for tname, rows in tables.items():
            if rows:
                store.write(tname, rows, ts)
        print(f"scan {ts}: " + ", ".join(f"{k}={len(v)}" for k, v in tables.items()), flush=True)
        if once:
            return 0
        time.sleep(interval)
