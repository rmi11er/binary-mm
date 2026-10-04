"""Metrics over collected spotcheck parquet: edge frame, buckets and per-product statistics.

Conventions
- Gap = venue mid - fair (per $1), fair from `analyze.fair_band` on the chosen option chain.
- Moneyness z = |ln K/F| / (sigma * sqrt T). sigma is the ATM IV the collector inverted with T to
  the chain's *own* expiry; T here is the time to the venue fix (the question horizon).
- Post-fix rows (ts >= fix) have no option rows, so fair uses the last pre-fix chain for that
  product and fix; F, sigma and T for the bucket are taken from that same pre-fix snapshot.
  Post-fix rows are excluded from every headline statistic (they are not a mispricing signal)
  and appear only in the post-fix section and in ttf-bucket breakdowns.
- Crossed ladder rows are kept in the frame but excluded from gap, taker and persistence stats.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from bmm.spotcheck.analyze import LadderRow, OptQuote, edges
from bmm.spotcheck.schema import FUTURES_SCHEMA, LADDER_SCHEMA, OPTIONS_SCHEMA
from bmm.spotcheck.vol import year_frac

MONEYNESS_BUCKETS = ("<0.5", "0.5-1", "1-2", ">2")  # |ln K/F| / (sigma*sqrt T)
TTF_BUCKETS = (">4h", "1-4h", "30-60m", "<30m", "post-fix")
EPS = 1e-12
H_NS = 3600 * 1_000_000_000
SUSPECT_REL_WIDTH = 0.5
SUSPECT_LAG_MS = 5000.0

EF_SCHEMA: dict[str, Any] = {
    "product": pl.String, "snapshot_id": pl.String, "ts_ns": pl.Int64, "fix_ts_ns": pl.Int64,
    "strike": pl.Float64, "fee": pl.Float64, "fair": pl.Float64, "lower": pl.Float64,
    "upper": pl.Float64, "yes_bid": pl.Float64, "yes_ask": pl.Float64, "mid_gap": pl.Float64,
    "take_buy_edge": pl.Float64, "take_sell_edge": pl.Float64, "arb_buy_edge": pl.Float64,
    "arb_sell_edge": pl.Float64, "bid_size": pl.Int64, "ask_size": pl.Int64,
    "F": pl.Float64, "sigma": pl.Float64, "T": pl.Float64, "m_bucket": pl.String,
    "ttf_bucket": pl.String, "expiry_mismatch": pl.Boolean, "crossed": pl.Boolean,
    "stale_chain": pl.Boolean, "opt_snapshot_id": pl.String, "opt_ts_ns": pl.Int64,
    "opt_expiry_ts_ns": pl.Int64,
}  # fmt: skip


def load(data_root: Path, d_from: date, d_to: date) -> dict[str, pl.DataFrame]:
    schemas = {"ladder": LADDER_SCHEMA, "futures": FUTURES_SCHEMA, "options": OPTIONS_SCHEMA}
    frames: dict[str, list[pl.DataFrame]] = {k: [] for k in schemas}
    d = d_from
    while d <= d_to:
        for name in schemas:
            p = Path(data_root) / f"date={d.isoformat()}" / f"{name}.parquet"
            if p.exists():
                frames[name].append(pl.read_parquet(p))
        d += timedelta(days=1)
    return {
        k: pl.concat(v) if v else pl.DataFrame(schema=schemas[k]) for k, v in frames.items()
    }


def moneyness_bucket(K: float, F: float, sigma: float | None, T: float) -> str:
    if sigma is None or not (sigma > 0 and T > 0 and F > 0 and K > 0):
        return "n/a"
    z = abs(math.log(K / F)) / (sigma * math.sqrt(T))
    if z < 0.5:
        return "<0.5"
    if z < 1:
        return "0.5-1"
    if z <= 2:
        return "1-2"
    return ">2"


def ttf_bucket(ts_ns: int, fix_ts_ns: int) -> str:
    dt = fix_ts_ns - ts_ns
    if dt <= 0:
        return "post-fix"
    if dt > 4 * H_NS:
        return ">4h"
    if dt > H_NS:
        return "1-4h"
    if dt >= H_NS // 2:
        return "30-60m"
    return "<30m"


@dataclass
class _Chain:
    snapshot_id: str
    ts_ns: int
    expiry_ts_ns: int
    mismatch: bool
    quotes: list[OptQuote]
    F: float
    sigma: float | None


def _select_chains(options: pl.DataFrame, fix_of: dict[str, int]) -> dict[str, _Chain]:
    """Per snapshot: the match chain if present, else the chain whose expiry is nearest the fix."""
    groups: dict[str, dict[tuple[int, str], list[dict[str, Any]]]] = defaultdict(dict)
    for r in options.iter_rows(named=True):
        groups[r["snapshot_id"]].setdefault((r["expiry_ts_ns"], r["expiry_role"]), []).append(r)
    out: dict[str, _Chain] = {}
    for sid, by_exp in groups.items():
        matches = [k for k in by_exp if k[1] == "match"]
        if matches:
            key = matches[0]
        else:
            fix = fix_of.get(sid)
            if fix is None:
                continue
            key = min(by_exp, key=lambda k: (abs(k[0] - fix), k[0]))
        rows = sorted(by_exp[key], key=lambda r: r["strike"])
        r0 = rows[0]
        out[sid] = _Chain(
            sid, r0["ts_ns"], key[0], bool(r0["expiry_mismatch"]) or key[1] != "match",
            [OptQuote(r["strike"], r["call_bid"], r["call_ask"]) for r in rows],
            r0["und_px"], r0["atm_sigma"],
        )  # fmt: skip
    return out


def edge_frame(tables: dict[str, pl.DataFrame], fee_per_side: float) -> pl.DataFrame:
    lad = tables["ladder"]
    snaps = lad.group_by("product", "snapshot_id", "fix_ts_ns").agg(pl.col("ts_ns").first())
    fix_of = {r["snapshot_id"]: r["fix_ts_ns"] for r in snaps.iter_rows(named=True)}
    chains = _select_chains(tables["options"], fix_of)
    by_snap = {k[0]: g for k, g in lad.partition_by("snapshot_id", as_dict=True).items()}
    out: list[dict[str, Any]] = []
    last_pre: dict[tuple[str, int], tuple[_Chain, float]] = {}
    for s in snaps.sort("ts_ns").iter_rows(named=True):
        sid, product, fix, ts = s["snapshot_id"], s["product"], s["fix_ts_ns"], s["ts_ns"]
        ch = chains.get(sid)
        if ts < fix:
            if ch is None:
                continue
            T = year_frac(ts, fix)
            last_pre[(product, fix)] = (ch, T)
            ref_ch, ref_T, stale = ch, T, False
        else:
            ref = last_pre.get((product, fix))
            if ref is None:
                continue
            ref_ch, ref_T = ref
            stale = ch is None
            ch = ref_ch if ch is None else ch
        rows = by_snap[sid].sort("strike").iter_rows(named=True)
        lrows = list(rows)
        ladder = [
            LadderRow(r["strike"], r["yes_bid"], r["yes_ask"], r["bid_size"] or 0,
                      r["ask_size"] or 0) for r in lrows
        ]  # fmt: skip
        by_k = {r["strike"]: r for r in lrows}
        for e in edges(ladder, ch.quotes, fee_per_side):
            lr = by_k[e.strike]
            out.append({
                "product": product, "snapshot_id": sid, "ts_ns": ts, "fix_ts_ns": fix,
                "strike": e.strike, "fee": fee_per_side, "fair": e.fair, "lower": e.lower,
                "upper": e.upper, "yes_bid": e.yes_bid, "yes_ask": e.yes_ask,
                "mid_gap": e.mid_gap, "take_buy_edge": e.take_buy_edge,
                "take_sell_edge": e.take_sell_edge, "arb_buy_edge": e.arb_buy_edge,
                "arb_sell_edge": e.arb_sell_edge, "bid_size": e.bid_size,
                "ask_size": e.ask_size, "F": ref_ch.F, "sigma": ref_ch.sigma, "T": ref_T,
                "m_bucket": moneyness_bucket(e.strike, ref_ch.F, ref_ch.sigma, ref_T),
                "ttf_bucket": ttf_bucket(ts, fix), "expiry_mismatch": ch.mismatch,
                "crossed": bool(lr["crossed"]), "stale_chain": stale,
                "opt_snapshot_id": ch.snapshot_id, "opt_ts_ns": ch.ts_ns,
                "opt_expiry_ts_ns": ch.expiry_ts_ns,
            })  # fmt: skip
    return pl.DataFrame(out, schema=EF_SCHEMA)
