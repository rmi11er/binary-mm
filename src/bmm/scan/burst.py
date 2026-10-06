"""High-frequency touch sampler for requote cadence. A 5-minute snapshot cannot resolve 5/30/60 s
requoting, so this polls the book of the top candidates every `every` seconds for a short burst.
Read-only: GET book only."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from bmm.clock import now_ns
from bmm.scan import config as cfgmod
from bmm.scan import store
from bmm.scan.http import GetOnlyClient

log = logging.getLogger("bmm.scan")


async def run_burst(
    adapters: Mapping[str, Any],
    markets: list[tuple[str, str]],
    seconds: float,
    every: float,
    root: Path = store.DEFAULT_ROOT,
    now: Callable[[], int] = now_ns,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = now()
    end = start + int(seconds * 1e9)

    async def one(venue: str, mkt: str) -> None:
        try:
            book = await adapters[venue].book(mkt)
        except Exception as e:  # noqa: BLE001 - one bad market must not stop the burst; logged
            log.warning("burst %s/%s skipped: %r", venue, mkt, e)
            return
        ts = now()  # stamp when the book arrived, not at tick start (throttled requests queue)
        b, a = (book.bids[0] if book.bids else None), (book.asks[0] if book.asks else None)
        rows.append(
            {"ts_ns": ts, "venue": venue, "market_id": mkt,
             "bid": b.price if b else None, "ask": a.price if a else None,
             "bid_size": b.size if b else None, "ask_size": a.size if a else None}
        )  # fmt: skip

    while now() < end:
        t = now()
        await asyncio.gather(*(one(v, m) for v, m in markets))
        await sleep(max(0.0, every - (now() - t) / 1e9))
    if rows:
        store.write("bursts", rows, start, root)
    return rows


def run(
    venues: list[str], top: int, seconds: float, every: float, root: Path = store.DEFAULT_ROOT
) -> int:
    from bmm.scan.venues import ADAPTERS

    conf = cfgmod.load()
    client = GetOnlyClient(set(conf.hosts), min_interval_s=0.12)
    ads = {
        n: ADAPTERS[n](client, conf.venue[n])
        for n in venues
        if n in conf.venue and conf.venue[n].enabled
    }
    ranked = [(v, m) for v, m in _ordered(root) if v in ads][:top]
    if not ranked:
        print("no candidates: run a full scan first (it writes the ranking)")
        return 1
    rows = asyncio.run(run_burst(ads, ranked, seconds, every, root))
    print(f"burst: {len(rows)} samples over {len(ranked)} markets, every {every}s for {seconds}s")
    return 0


def _ordered(root: Path) -> list[tuple[str, str]]:
    r = store.read("ranking", None, root)
    if r.is_empty():
        return []
    r = r.filter(r["ts_ns"] == r["ts_ns"].max()).sort("rank")
    return [(v, m) for v, m in r.select("venue", "market_id").iter_rows()]
