"""Polymarket US futures depth analysis: per market and side, how much competing depth stands between us and
the (pro-rated) reward pool. Read-only: GET books only.

Definitions (all per side of the book, levels walked best price outward):
- touch_depth: size at the best price level.
- depth_target: resting size walked until the program's target size is reached (raw size, capped at target).
- largest_level / largest_share: the largest single price level inside that walk and its share of
  depth_target; one_level is set when it holds > 80%. largest_offset_ticks = 0 means it sits at the touch.
- D: competing scored depth (discount^ticks x size) inside the walk; our share at size s is s / (s + D),
  with our order at the best price (weight 1).
- R_side_day: the pro-rated event pool per market per day x the side split (0.5, unverified).
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bmm.clock import now_ns
from bmm.scan import config as cfgmod
from bmm.scan import store
from bmm.scan.http import GetOnlyClient
from bmm.scan.models import Book, Level, Program
from bmm.scan.venues.common import geo_walk

log = logging.getLogger("bmm.scan")
ONE_LEVEL = 0.8


def depth_metrics(
    levels: list[Level], target: float, discount: float, tick: float = 0.01
) -> dict[str, Any]:
    if not levels:
        return {"touch_depth": 0.0, "depth_target": 0.0, "largest_level": 0.0, "largest_price": None,
                "largest_offset_ticks": None, "largest_share": 0.0, "one_level": False, "D": 0.0, "empty": True}  # fmt: skip
    rem = float(target)
    taken: list[tuple[float, float]] = []
    for lv in levels:
        take = min(lv.size, rem)
        if take > 0:
            taken.append((lv.price, take))
            rem -= take
        if rem <= 0:
            break
    depth = sum(t for _, t in taken)
    price, big = max(taken, key=lambda x: x[1]) if taken else (None, 0.0)
    share = big / depth if depth > 0 else 0.0
    return {
        "touch_depth": levels[0].size, "depth_target": depth, "largest_level": big, "largest_price": price,
        "largest_offset_ticks": round(abs(price - levels[0].price) / tick) if price is not None else None,
        "largest_share": share, "one_level": depth > 0 and share > ONE_LEVEL,
        "D": geo_walk(levels, target, discount, tick), "empty": False,
    }  # fmt: skip


def our_share(s: float, d: float) -> float:
    """Pro-rata share of the scored depth when we post s at the best price."""
    if s <= 0:
        return 0.0
    return s / (s + d) if s + d > 0 else 1.0


def analyze_futures(
    programs: list[Program], books: Mapping[str, Book], side_split: float = 0.5, tick: float = 0.01
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in programs:
        b = books.get(p.market_id)
        if b is None:
            continue
        target = p.target_size or 0.0
        r_day = p.R_usd_hr * 24 * side_split
        for side, levels in (("bid", b.bids), ("ask", b.asks)):
            m = depth_metrics(levels, target, p.discount or 1.0, tick)
            st, s1k = our_share(target, m["D"]), our_share(1000, m["D"])
            rows.append({"key": f"{p.market_id}:{side}", "market_id": p.market_id, "event_key": p.event_key,
                         "program_id": p.program_id, "side": side, "target": target, "event_n": p.event_n,
                         "R_side_day": r_day, **m, "share_target": st, "share_1000": s1k,
                         "day_target": r_day * st, "day_1000": r_day * s1k})  # fmt: skip
    return rows


def rank_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best first by R_side_day / depth_target (depth floored at 1 contract so an empty side stays finite)."""
    scored = [{**r, "score": r["R_side_day"] / max(r["depth_target"], 1.0)} for r in rows]
    return sorted(scored, key=lambda r: (-r["score"], r.get("key", "")))


async def collect(adapter: Any, programs: list[Program], concurrency: int = 4) -> dict[str, Book]:
    sem = asyncio.Semaphore(concurrency)
    books: dict[str, Book] = {}

    async def one(p: Program) -> None:
        async with sem:
            try:
                books[p.market_id] = await adapter.book(p.market_id)
            except Exception as e:  # noqa: BLE001 - one bad market must not stop the sweep; logged
                log.warning("futures-depth %s skipped: %r", p.market_id, e)

    await asyncio.gather(*(one(p) for p in programs))
    return books


def _fmt(r: dict[str, Any]) -> str:
    big = (
        f"{r['largest_level']:,.0f}@{r['largest_offset_ticks']}t ({r['largest_share']:.0%})"
        if not r["empty"]
        else "-"
    )
    fl = ",".join(f for f, on in (("ONE_LEVEL>80%", r["one_level"]), ("EMPTY", r["empty"]),
                                  ("TARGET_UNREACHED", not r["empty"] and r["depth_target"] < r["target"])) if on)  # fmt: skip
    return (f"{r['market_id'][:46]:46} {r['side']:3} R_side=${r['R_side_day']:7.3f}/d touch={r['touch_depth']:>9,.0f} "
            f"depth@target={r['depth_target']:>8,.0f} largest={big:22} share@T={r['share_target']:.1%} "
            f"share@1k={r['share_1000']:.1%} ${r['day_1000']:.3f}/d@1k {fl}")  # fmt: skip


def render(
    rows: list[dict[str, Any]], programs: list[Program], top: int = 20, worst: int = 10
) -> str:
    ranked = rank_rows(rows)
    by_prog: defaultdict[str, list[Program]] = defaultdict(list)
    for p in programs:
        by_prog[p.program_id].append(p)
    out = [
        "PROGRAMS (futures = daily_event): program | target size | discount | markets | events | pool per event $/day | instruments/event (median, max)"
    ]
    for pid, ps in sorted(by_prog.items(), key=lambda kv: -len(kv[1])):
        ev: defaultdict[str, int] = defaultdict(int)
        for p in ps:
            ev[p.event_key] += 1
        out.append(f"  {pid[:36]:36} target={ps[0].target_size:,.0f} discount={ps[0].discount} markets={len(ps)} events={len(ev)} "
                   f"pool/event=${ps[0].pool_event_usd:,.0f} instruments/event median={statistics.median(ev.values()):.0f} max={max(ev.values())}")  # fmt: skip
    head = f"\nRANKED by R_side/day / depth within target: {len(ranked)} market-sides; "
    head += f"one-level flagged: {sum(r['one_level'] for r in ranked)}; empty sides: {sum(r['empty'] for r in ranked)}"
    out += [head, f"\n{top} BEST", *[_fmt(r) for r in ranked[:top]]]
    out += [f"\n{worst} WORST", *[_fmt(r) for r in ranked[-worst:]]]
    return "\n".join(out)


def run(top: int = 20, worst: int = 10, limit: int | None = None, root: Path = store.DEFAULT_ROOT,
        out_dir: Path = Path("reports")) -> int:  # fmt: skip
    from bmm.scan.venues.pm_us import PmUs

    conf = cfgmod.load()
    cfg = conf.venue["pm_us"]
    ad = PmUs(GetOnlyClient(set(conf.hosts)), cfg)

    async def main() -> tuple[list[Program], dict[str, Book]]:
        progs = [p for p in await ad.all_programs() if p.period == "daily_event"]
        if limit:
            progs = progs[:limit]
        return progs, await collect(ad, progs, 4)

    progs, books = asyncio.run(main())
    rows = analyze_futures(
        progs,
        books,
        float(cfg.raw.get("side_split", 0.5)),
        float(cfg.raw.get("scoring_tick", 0.01)),
    )
    ts = now_ns()
    if rows:
        store.write("futures_depth", rows, ts, root)
    text = render(rows, progs, top, worst)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pm_futures_depth.txt").write_text(text)
    print(text)
    return 0
