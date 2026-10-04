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
    return {k: pl.concat(v) if v else pl.DataFrame(schema=schemas[k]) for k, v in frames.items()}


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


# ------------------------------------------------------------------ statistics


def _f(x: Any) -> float:
    return float(x) if x is not None else 0.0


def _headline(ef: pl.DataFrame) -> pl.DataFrame:
    """Pre-fix, non-crossed rows (the population every headline statistic uses)."""
    return ef.filter((pl.col("ttf_bucket") != "post-fix") & ~pl.col("crossed"))


def _order(df: pl.DataFrame) -> pl.DataFrame:
    keys = [c for c in ("product", "m_bucket", "ttf_bucket") if c in df.columns]
    return df.sort(keys)


def gap_stats(ef: pl.DataFrame, by: tuple[str, ...] = ("m_bucket",)) -> pl.DataFrame:
    """Per product x `by`: n, median_abs_gap, iqr, pct_gt_2c, pct_gt_3c (all on |gap|).

    Crossed rows are always excluded. Post-fix rows are excluded unless "ttf_bucket" is in `by`.
    """
    d = ef.filter(~pl.col("crossed") & pl.col("mid_gap").is_not_null())
    if "ttf_bucket" not in by:
        d = d.filter(pl.col("ttf_bucket") != "post-fix")
    d = d.with_columns(g=pl.col("mid_gap").abs())
    res = d.group_by("product", *by).agg(
        n=pl.len(),
        median_abs_gap=pl.col("g").median(),
        iqr=pl.col("g").quantile(0.75, "linear") - pl.col("g").quantile(0.25, "linear"),
        pct_gt_2c=(pl.col("g") > 0.02 + EPS).mean(),
        pct_gt_3c=(pl.col("g") > 0.03 + EPS).mean(),
    )
    return _order(res)


def size_at_gap_strikes(ef: pl.DataFrame, thr: float = 0.03) -> pl.DataFrame:
    """Per product x m_bucket: median displayed size on the taker side at strikes with |gap| > thr
    (bid size when the venue is rich, ask size when cheap)."""
    d = _headline(ef).filter(pl.col("mid_gap").abs() > thr + EPS)
    d = d.with_columns(
        sz=pl.when(pl.col("mid_gap") > 0).then(pl.col("bid_size")).otherwise(pl.col("ask_size"))
    )
    res = d.group_by("product", "m_bucket").agg(
        median_size=pl.col("sz").median().cast(pl.Float64), n=pl.len()
    )
    return _order(res)


def taker_stats(ef: pl.DataFrame, fees: tuple[float, ...] = (0.01, 0.02)) -> pl.DataFrame:
    """Per product x fee. Take edges are linear in fee, so any fee is derived from `ef.fee`.

    A snapshot's best edge = max over strikes of max(buy, sell). capturable = sum of positive
    edge x displayed size on the taker side, per snapshot.
    """
    d = _headline(ef)
    rows: list[dict[str, Any]] = []
    for f in fees:
        shift = pl.lit(f) - pl.col("fee")
        x = d.with_columns(b=pl.col("take_buy_edge") - shift, s=pl.col("take_sell_edge") - shift)
        x = x.with_columns(
            best=pl.max_horizontal("b", "s"),
            cap=pl.when(pl.col("b") > 0).then(pl.col("b") * pl.col("ask_size")).otherwise(0.0)
            + pl.when(pl.col("s") > 0).then(pl.col("s") * pl.col("bid_size")).otherwise(0.0),
        )
        per = x.group_by("product", "snapshot_id").agg(
            best=pl.col("best").max(), cap=pl.col("cap").sum()
        ).filter(pl.col("best").is_not_null())  # fmt: skip
        for (product,), g in per.group_by("product"):
            rows.append({
                "product": product, "fee": f, "n_snapshots": g.height,
                "pct_snapshots_best_edge_gt_1c": _f((g["best"] > 0.01 + EPS).mean()),
                "pct_snapshots_best_edge_gt_2c": _f((g["best"] > 0.02 + EPS).mean()),
                "pct_snapshots_best_edge_le_0": _f((g["best"] <= EPS).mean()),
                "capturable_median": _f(g["cap"].median()),
                "capturable_p90": _f(g["cap"].quantile(0.9, "linear")),
            })  # fmt: skip
    schema = {
        "product": pl.String, "fee": pl.Float64, "n_snapshots": pl.Int64,
        "pct_snapshots_best_edge_gt_1c": pl.Float64, "pct_snapshots_best_edge_gt_2c": pl.Float64,
        "pct_snapshots_best_edge_le_0": pl.Float64, "capturable_median": pl.Float64,
        "capturable_p90": pl.Float64,
    }  # fmt: skip
    return pl.DataFrame(rows, schema=schema).sort("product", "fee")


def persistence(ef: pl.DataFrame, thr: float = 0.03) -> pl.DataFrame:
    """Runs of consecutive snapshots with |gap| > thr, per (product, strike).

    "Consecutive" = adjacent snapshot ordinals within the product; a missing row, a crossed row
    or |gap| <= thr breaks the run. A run is attributed to the moneyness bucket of its first
    snapshot. Only pre-fix rows are used.
    """
    d = ef.filter(ef["ttf_bucket"] != "post-fix")
    runs: dict[tuple[str, str], list[int]] = defaultdict(list)
    for pkey, g in d.group_by("product"):
        pname = str(pkey[0])
        ordinal = {t: i for i, t in enumerate(sorted(g["ts_ns"].unique().to_list()))}
        by_strike: dict[float, list[dict[str, Any]]] = defaultdict(list)
        for r in g.sort("ts_ns").iter_rows(named=True):
            by_strike[r["strike"]].append(r)
        for rows in by_strike.values():
            cur = 0
            first_bucket = ""
            prev = -10
            for r in rows:
                o = ordinal[r["ts_ns"]]
                hit = (
                    (not r["crossed"])
                    and r["mid_gap"] is not None
                    and abs(r["mid_gap"]) > thr + EPS
                )
                if hit and cur and o == prev + 1:
                    cur += 1
                elif hit:
                    if cur:
                        runs[(pname, first_bucket)].append(cur)
                    cur, first_bucket = 1, r["m_bucket"]
                else:
                    if cur:
                        runs[(pname, first_bucket)].append(cur)
                    cur = 0
                prev = o
            if cur:
                runs[(pname, first_bucket)].append(cur)
    out = []
    for (product, bucket), rl in sorted(runs.items()):
        s = sorted(rl)
        mid = len(s) // 2
        med = float(s[mid]) if len(s) % 2 else 0.5 * (s[mid - 1] + s[mid])
        out.append({"product": product, "m_bucket": bucket, "n_runs": len(s),
                    "median_run": med, "max_run": max(s)})  # fmt: skip
    schema = {"product": pl.String, "m_bucket": pl.String, "n_runs": pl.Int64,
              "median_run": pl.Float64, "max_run": pl.Int64}  # fmt: skip
    return pl.DataFrame(out, schema=schema)


def _opt_widths(options: pl.DataFrame) -> dict[tuple[str, int], list[tuple[float, float, float]]]:
    out: dict[tuple[str, int], list[tuple[float, float, float]]] = defaultdict(list)
    for r in options.sort("strike").iter_rows(named=True):
        out[(r["snapshot_id"], r["expiry_ts_ns"])].append(
            (r["strike"], r["call_bid"], r["call_ask"])
        )
    return out


def arb_table(ef: pl.DataFrame, tables: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Rows with a positive model-free edge. opt_rel_width = widest (ask-bid)/mid among the (up to
    four) option strikes nearest the venue strike; suspect if > 0.5 or the option quote is more
    than 5 s older than the ladder."""
    d = ef.filter(
        ~pl.col("crossed") & ((pl.col("arb_buy_edge") > 0) | (pl.col("arb_sell_edge") > 0))
    )
    chains = _opt_widths(tables["options"])
    rows = []
    for r in d.iter_rows(named=True):
        q = chains.get((r["opt_snapshot_id"], r["opt_expiry_ts_ns"]), [])
        below = [x for x in q if x[0] <= r["strike"]][-2:]
        above = [x for x in q if x[0] >= r["strike"]][:2]
        w = [(a - b) / max(0.5 * (a + b), 1e-9) for _k, b, a in below + above]
        rel = max(w) if w else float("inf")
        lag = (r["ts_ns"] - r["opt_ts_ns"]) / 1e6
        rows.append({
            "product": r["product"], "snapshot_id": r["snapshot_id"], "ts_ns": r["ts_ns"],
            "strike": r["strike"], "yes_bid": r["yes_bid"], "yes_ask": r["yes_ask"],
            "arb_buy_edge": r["arb_buy_edge"], "arb_sell_edge": r["arb_sell_edge"],
            "opt_ts_lag_ms": lag, "opt_rel_width": rel,
            "suspect": rel > SUSPECT_REL_WIDTH or lag > SUSPECT_LAG_MS,
        })  # fmt: skip
    schema = {
        "product": pl.String, "snapshot_id": pl.String, "ts_ns": pl.Int64, "strike": pl.Float64,
        "yes_bid": pl.Float64, "yes_ask": pl.Float64, "arb_buy_edge": pl.Float64,
        "arb_sell_edge": pl.Float64, "opt_ts_lag_ms": pl.Float64, "opt_rel_width": pl.Float64,
        "suspect": pl.Boolean,
    }  # fmt: skip
    return pl.DataFrame(rows, schema=schema)


def _volume_steps(lad: pl.DataFrame) -> pl.DataFrame:
    """One row per positive volume step per (product, strike): ts, delta, last."""
    d = lad.filter(pl.col("volume").is_not_null()).sort("product", "strike", "ts_ns")
    d = d.with_columns(dv=pl.col("volume") - pl.col("volume").shift(1).over("product", "strike"))
    return d.filter(pl.col("dv") > 0)


def ladder_quality(tables: dict[str, pl.DataFrame]) -> pl.DataFrame:
    lad = tables["ladder"]
    steps = (
        _volume_steps(lad)
        .group_by("product")
        .agg(trades_seen=pl.len(), volume_delta=pl.col("dv").sum())
    )
    ok = lad.filter(~pl.col("crossed"))
    res = lad.group_by("product").agg(
        n_rows=pl.len(),
        strikes_quoted=pl.col("strike")
        .filter(pl.col("yes_bid").is_not_null() | pl.col("yes_ask").is_not_null())
        .n_unique(),
        crossed_count=pl.col("crossed").sum(),
    )
    q = ok.group_by("product").agg(
        median_width=(pl.col("yes_ask") - pl.col("yes_bid")).median(),
        median_depth=(pl.col("bid_size") + pl.col("ask_size")).median(),  # both sides, per row
    )
    out = res.join(q, on="product", how="left").join(steps, on="product", how="left")
    return out.with_columns(
        pl.col("trades_seen").fill_null(0), pl.col("volume_delta").fill_null(0.0)
    ).sort("product")


def post_fix_activity(
    tables: dict[str, pl.DataFrame], ef: pl.DataFrame | None = None
) -> pl.DataFrame:
    """Per product: ladder rows with a resting quote after the fix, trades after the fix (volume
    steps, since trades are only visible as volume increases) and their price vs the last pre-fix
    fair at that strike (needs `ef`; null otherwise)."""
    lad = tables["ladder"]
    post = lad.filter(pl.col("ts_ns") >= pl.col("fix_ts_ns"))
    resting = post.filter(
        pl.col("yes_bid").is_not_null() | pl.col("yes_ask").is_not_null()
    ).group_by("product").agg(quotes_resting=pl.len())  # fmt: skip
    steps = _volume_steps(lad).filter(pl.col("ts_ns") >= pl.col("fix_ts_ns"))
    if ef is not None and ef.height:
        pre = (_headline(ef).sort("ts_ns").group_by("product", "strike", maintain_order=True)
               .agg(pre_fair=pl.col("fair").last()))  # fmt: skip
        steps = steps.join(pre, on=["product", "strike"], how="left").with_columns(
            px_vs_fair=pl.col("last") - pl.col("pre_fair")
        )
    else:
        steps = steps.with_columns(px_vs_fair=pl.lit(None, dtype=pl.Float64))
    tr = steps.group_by("product").agg(
        trades_after_fix=pl.len(), volume_after_fix=pl.col("dv").sum(),
        median_trade_px_vs_fair=pl.col("px_vs_fair").median(),
    )  # fmt: skip
    prods = lad.select("product").unique()
    out = prods.join(resting, on="product", how="left").join(tr, on="product", how="left")
    return out.with_columns(
        pl.col("quotes_resting").fill_null(0), pl.col("trades_after_fix").fill_null(0),
        pl.col("volume_after_fix").fill_null(0.0),
    ).sort("product")  # fmt: skip


def mismatch_share(ef: pl.DataFrame) -> pl.DataFrame:
    """Per product: share of snapshots on a mismatched chain, and signed/abs median gap matched
    vs mismatched. bias_abs = median|gap| mismatched - matched (> 0: mismatched chains look
    richer than matched ones). Signed medians are shown too but cancel when the venue is rich on
    one wing and cheap on the other."""
    d = _headline(ef).filter(pl.col("mid_gap").is_not_null())
    d = d.with_columns(g=pl.col("mid_gap").abs())
    snaps = d.group_by("product", "snapshot_id").agg(mm=pl.col("expiry_mismatch").first())
    share = snaps.group_by("product").agg(
        share_mismatched=pl.col("mm").mean(), n_snapshots=pl.len()
    )

    def med(flag: bool, col: str, name: str) -> pl.DataFrame:
        return (
            d.filter(pl.col("expiry_mismatch") == flag)
            .group_by("product")
            .agg(pl.col(col).median().alias(name))
        )

    out = share
    for flag, tag in ((False, "matched"), (True, "mismatched")):
        out = out.join(med(flag, "mid_gap", f"median_gap_{tag}"), on="product", how="left")
        out = out.join(med(flag, "g", f"median_abs_gap_{tag}"), on="product", how="left")
    return out.with_columns(
        bias_abs=pl.col("median_abs_gap_mismatched") - pl.col("median_abs_gap_matched")
    ).sort("product")
