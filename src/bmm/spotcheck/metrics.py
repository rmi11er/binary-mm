"""Metrics over collected spotcheck parquet: edge frame, buckets and per-product statistics.

Conventions
- Gap = venue mid - fair (per $1), fair from `analyze.fair_band` on the chosen option chain.
- Moneyness z = |ln K/F| / (sigma * sqrt T). sigma is the ATM IV the collector inverted with T to
  the chain's *own* expiry; T here is the time to the venue fix (the question horizon).
- Post-fix rows (ts >= fix): matched chains have no option rows, and a mismatched later chain
  may still have some but they are deliberately ignored. Fair always uses the last pre-fix chain
  for that product and fix; F, sigma and T for the bucket come from that same pre-fix snapshot.
  Post-fix rows are excluded from every headline statistic (they are not a mispricing signal)
  and appear only in the post-fix section and in ttf-bucket breakdowns.
- Crossed ladder rows are kept in the frame but excluded from gap, taker and persistence stats.
- Simultaneity (recomputed here from und_px_before/after; the collector's df_flag is information
  only): a snapshot is "non-simultaneous" when |dF| x digital delta at the ATM ladder strike
  exceeds the report's threshold (default 1c), or when dF or sigma is unknown (no futures row or
  a null quote counts as flagged). Both buckets are kept and reported. For every row the fair band is widened by
  w = |digital_delta(F, K_row, sigma, T_fix)| * |und_px_after - und_px_before| (K is the row's own
  strike; sigma is the chain's ATM sigma, else the product's last resolved one; F is the mean of
  the two quotes), lower_w = clip(lower - w), upper_w = clip(upper + w), and the model-free arb
  edges are recomputed on the widened band. gap_outside = sign(gap) * max(|gap| - w, 0). w is null
  when dF or sigma is unknown (and for post-fix rows).
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
from bmm.spotcheck.vol import digital_delta, year_frac

MONEYNESS_BUCKETS = ("<0.5", "0.5-1", "1-2", ">2")  # |ln K/F| / (sigma*sqrt T)
TTF_BUCKETS = (">4h", "1-4h", "30-60m", "<30m", "post-fix")
EPS = 1e-12
H_NS = 3600 * 1_000_000_000
SUSPECT_REL_WIDTH = 0.5
ARB_MAX_LAG_S = 60  # option fetch vs ladder fetch: beyond this a row is suspect on lag

EF_SCHEMA: dict[str, Any] = {
    "product": pl.String, "snapshot_id": pl.String, "ts_ns": pl.Int64, "fix_ts_ns": pl.Int64,
    "strike": pl.Float64, "fee": pl.Float64, "fair": pl.Float64, "lower": pl.Float64,
    "upper": pl.Float64, "yes_bid": pl.Float64, "yes_ask": pl.Float64, "mid_gap": pl.Float64,
    "take_buy_edge": pl.Float64, "take_sell_edge": pl.Float64, "arb_buy_edge": pl.Float64,
    "arb_sell_edge": pl.Float64, "bid_size": pl.Int64, "ask_size": pl.Int64,
    "F": pl.Float64, "sigma": pl.Float64, "T": pl.Float64, "m_bucket": pl.String,
    "ttf_bucket": pl.String, "expiry_mismatch": pl.Boolean, "crossed": pl.Boolean,
    "stale_chain": pl.Boolean, "df_flag": pl.Boolean, "sim_bucket": pl.String,
    "dF": pl.Float64, "w": pl.Float64, "lower_w": pl.Float64, "upper_w": pl.Float64,
    "arb_buy_edge_w": pl.Float64, "arb_sell_edge_w": pl.Float64, "gap_outside": pl.Float64, "opt_snapshot_id": pl.String, "opt_ts_ns": pl.Int64,
    "opt_expiry_ts_ns": pl.Int64, "ladder_ts_ns": pl.Int64, "payout_scale": pl.Float64,
    "expiry_role": pl.String,
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
    role: str = "match"
    opt_ts_ns: int = 0  # when the option fetch completed (falls back to the slot ts)


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
            r0["und_px"], r0["atm_sigma"], key[1],
            r0["opt_ts_ns"] if r0.get("opt_ts_ns") is not None else r0["ts_ns"],
        )  # fmt: skip
    return out


def edge_frame(
    tables: dict[str, pl.DataFrame], fee_per_side: float, nonsim_threshold: float = 0.01
) -> pl.DataFrame:
    """One row per (snapshot, strike). `sim_bucket` is recomputed here (the collector's df_flag is
    kept as information): a pre-fix snapshot is non-simultaneous when |und_px_after - und_px_before|
    x digital delta (F mean, ATM ladder strike, sigma as for w, T to the fix) exceeds
    `nonsim_threshold`, and also when dF or sigma is unknown (no futures row, null quote)."""
    lad = tables["ladder"]
    snaps = lad.group_by("product", "snapshot_id", "fix_ts_ns").agg(pl.col("ts_ns").first())
    fix_of = {r["snapshot_id"]: r["fix_ts_ns"] for r in snaps.iter_rows(named=True)}
    chains = _select_chains(tables["options"], fix_of)
    fut = tables["futures"]
    # per snapshot: (und_px_before, und_px_after, df_flag); df_flag is information only here.
    fut_info: dict[str, tuple[float | None, float | None, bool | None]] = {}
    if fut.height and "df_flag" in fut.columns:
        for r in fut.iter_rows(named=True):
            fut_info[r["snapshot_id"]] = (r.get("und_px_before"), r.get("und_px_after"),
                                          r["df_flag"])  # fmt: skip
    sigma_memo: dict[tuple[str, int], float] = {}
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
            stale = True  # post-fix: fair always from the last pre-fix chain
            ch = ref_ch
        fb, fa = (fut_info[sid][0], fut_info[sid][1]) if sid in fut_info else (None, None)
        dF = abs(fa - fb) if fa is not None and fb is not None else None
        F_row = 0.5 * (fa + fb) if fa is not None and fb is not None else None
        if ref_ch.sigma is not None and ts < fix:
            sigma_memo[(product, fix)] = ref_ch.sigma
        sig_w = ref_ch.sigma if ref_ch.sigma is not None else sigma_memo.get((product, fix))
        lrows = list(by_snap[sid].sort("strike").iter_rows(named=True))
        if ts >= fix:
            sim = True  # settled: not compared with the surface
        elif dF is None or F_row is None or sig_w is None or F_row <= 0:
            sim = False  # unknown move or sigma: non-simultaneous (null means flagged)
        else:
            atm_k = min((r["strike"] for r in lrows), key=lambda k: abs(k - F_row))
            sim = dF * abs(digital_delta(F_row, atm_k, sig_w, ref_T)) <= nonsim_threshold + 1e-12
        ladder = [
            LadderRow(r["strike"], r["yes_bid"], r["yes_ask"], r["bid_size"] or 0,
                      r["ask_size"] or 0) for r in lrows
        ]  # fmt: skip
        by_k = {r["strike"]: r for r in lrows}
        for e in edges(ladder, ch.quotes, fee_per_side):
            lr = by_k[e.strike]
            w: float | None = None
            if (dF is not None and F_row is not None and sig_w is not None and ts < fix
                    and e.strike > 0 and F_row > 0):  # fmt: skip
                w = abs(digital_delta(F_row, e.strike, sig_w, ref_T)) * dF
            ww = 0.0 if w is None else w
            lo_w, up_w = min(max(e.lower - ww, 0.0), 1.0), min(max(e.upper + ww, 0.0), 1.0)
            fee = fee_per_side
            gout = (
                None if w is None or e.mid_gap is None
                else math.copysign(max(abs(e.mid_gap) - w, 0.0), e.mid_gap)
            )  # fmt: skip
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
                "df_flag": fut_info[sid][2] if sid in fut_info else False,
                "sim_bucket": "simultaneous" if sim else "non-simultaneous",
                "dF": dF, "w": w, "lower_w": lo_w, "upper_w": up_w,
                "arb_buy_edge_w": None if e.yes_ask is None else lo_w - e.yes_ask - fee,
                "arb_sell_edge_w": None if e.yes_bid is None else e.yes_bid - up_w - fee,
                "gap_outside": gout,
                "opt_snapshot_id": ch.snapshot_id, "opt_ts_ns": ch.opt_ts_ns,
                "opt_expiry_ts_ns": ch.expiry_ts_ns,
                "ladder_ts_ns": lr["ladder_ts_ns"] if lr["ladder_ts_ns"] is not None else ts,
                "payout_scale": lr["payout_scale"] if lr["payout_scale"] is not None else 1.0,
                "expiry_role": ch.role,
            })  # fmt: skip
    return pl.DataFrame(out, schema=EF_SCHEMA)


# ------------------------------------------------------------------ statistics


def _f(x: Any) -> float:
    return float(x) if x is not None else 0.0


def _headline(ef: pl.DataFrame, sim: str | None = "simultaneous") -> pl.DataFrame:
    """Pre-fix, non-crossed rows of one simultaneity bucket (None or "adjusted" = both)."""
    d = ef.filter((pl.col("ttf_bucket") != "post-fix") & ~pl.col("crossed"))
    return d if sim in (None, "adjusted") else d.filter(pl.col("sim_bucket") == sim)


def _adjusted(d: pl.DataFrame) -> pl.DataFrame:
    """Column `adj`: mid_gap for simultaneous rows, gap_outside for non-simultaneous ones."""
    return d.with_columns(
        adj=pl.when(pl.col("sim_bucket") == "simultaneous")
        .then(pl.col("mid_gap"))
        .otherwise(pl.col("gap_outside"))
    )


def _order(df: pl.DataFrame) -> pl.DataFrame:
    keys = [c for c in ("product", "m_bucket", "ttf_bucket") if c in df.columns]
    return df.sort(keys)


def gap_stats(
    ef: pl.DataFrame,
    by: tuple[str, ...] = ("m_bucket",),
    sim: str = "simultaneous",
    value: str = "mid_gap",
    floor_rows: bool = True,
) -> pl.DataFrame:
    """Per product x `by`: n, median_abs_gap, iqr, pct_gt_2c, pct_gt_3c (per observation, i.e. per
    strike-snapshot, on |value|) and pct_snapshots_gt_2c/3c (share of snapshots whose median |value|
    over the group exceeds the threshold). The rule uses the per-observation columns.

    `sim` selects the bucket: "simultaneous", "non-simultaneous" or "adjusted" (every snapshot;
    non-simultaneous rows contribute gap_outside, i.e. gap beyond what the future move explains,
    simultaneous rows their mid_gap), "all" (every snapshot, unadjusted mid_gap). `value` is
    "mid_gap" or "gap_outside". `floor_rows=False` drops rows whose fair is < 0.01 or > 0.99
    (tick-floor rows). Crossed rows are always excluded; post-fix rows are excluded unless
    "ttf_bucket" is in `by`.
    """
    d = ef.filter(~pl.col("crossed"))
    if sim == "adjusted":
        d = d.with_columns(
            v=pl.when(pl.col("sim_bucket") == "simultaneous")
            .then(pl.col("mid_gap"))
            .otherwise(pl.col("gap_outside"))
        )
    elif sim == "all":
        d = d.with_columns(v=pl.col("mid_gap"))
    else:
        d = d.filter(pl.col("sim_bucket") == sim).with_columns(v=pl.col(value))
    d = d.filter(pl.col("v").is_not_null())
    if not floor_rows:
        d = d.filter((pl.col("fair") >= 0.01) & (pl.col("fair") <= 0.99))
    if "ttf_bucket" not in by:
        d = d.filter(pl.col("ttf_bucket") != "post-fix")
    d = d.with_columns(g=pl.col("v").abs())
    res = d.group_by("product", *by).agg(
        n=pl.len(),
        median_abs_gap=pl.col("g").median(),
        iqr=pl.col("g").quantile(0.75, "linear") - pl.col("g").quantile(0.25, "linear"),
        pct_gt_2c=(pl.col("g") > 0.02 + EPS).mean(),
        pct_gt_3c=(pl.col("g") > 0.03 + EPS).mean(),
    )
    snap = d.group_by("product", "snapshot_id", *by).agg(m=pl.col("g").median())
    ps = snap.group_by("product", *by).agg(
        n_snapshots=pl.len(),
        pct_snapshots_gt_2c=(pl.col("m") > 0.02 + EPS).mean(),
        pct_snapshots_gt_3c=(pl.col("m") > 0.03 + EPS).mean(),
    )
    return _order(res.join(ps, on=["product", *by], how="left"))


def nonsim_extra(ef: pl.DataFrame, by: tuple[str, ...] = ("m_bucket",)) -> pl.DataFrame:
    """Non-simultaneous bucket: median band widening w and median fetch-interval move dF."""
    d = _headline(ef, "non-simultaneous")
    res = d.group_by("product", *by).agg(
        median_w=pl.col("w").median(),
        median_dF=pl.col("dF").median(),
        n_w_null=pl.col("w").is_null().sum(),
    )
    return _order(res)


def sim_share(ef: pl.DataFrame) -> pl.DataFrame:
    """Per product x ttf bucket (pre-fix): snapshots, simultaneous snapshots and their share."""
    d = (
        _headline(ef, None)
        .group_by("product", "ttf_bucket", "snapshot_id")
        .agg(sim=(pl.col("sim_bucket") == "simultaneous").all())
    )
    res = d.group_by("product", "ttf_bucket").agg(
        snapshots=pl.len(), simultaneous=pl.col("sim").sum(), share=pl.col("sim").mean()
    )
    return _order(res)


def sim_counts(ef: pl.DataFrame) -> pl.DataFrame:
    """Snapshots and rows per product x simultaneity bucket (pre-fix, non-crossed)."""
    d = _headline(ef, None)
    return (
        d.group_by("product", "sim_bucket")
        .agg(snapshots=pl.col("snapshot_id").n_unique(), rows=pl.len())
        .sort("product", "sim_bucket")
    )


def size_at_gap_strikes(
    ef: pl.DataFrame, thr: float = 0.03, sim: str = "simultaneous", value: str = "mid_gap"
) -> pl.DataFrame:
    """Per product x m_bucket: median displayed size on the taker side at strikes with |gap| > thr
    (bid size when the venue is rich, ask size when cheap)."""
    d = _adjusted(_headline(ef, sim))
    col = "adj" if sim == "adjusted" else value
    d = d.filter(pl.col(col).abs() > thr + EPS)
    d = d.with_columns(
        sz=pl.when(pl.col(col) > 0).then(pl.col("bid_size")).otherwise(pl.col("ask_size"))
    )
    res = d.group_by("product", "m_bucket").agg(
        median_size=pl.col("sz").median().cast(pl.Float64), n=pl.len()
    )
    return _order(res)


def taker_stats(
    ef: pl.DataFrame, fees: tuple[float, ...] = (0.01, 0.02), sim: str = "simultaneous"
) -> pl.DataFrame:
    """Per product x fee. Take edges are linear in fee, so any fee is derived from `ef.fee`.

    A snapshot's best edge = max over strikes of max(buy, sell). capturable = sum of positive
    edge x displayed size on the taker side, per snapshot. `sim` selects the simultaneity bucket;
    take edges are against the point fair (not the widened band) in both.
    """
    d = _headline(ef, sim)
    rows: list[dict[str, Any]] = []
    for f in fees:
        shift = pl.lit(f) - pl.col("fee")
        x = d.with_columns(b=pl.col("take_buy_edge") - shift, s=pl.col("take_sell_edge") - shift)
        x = x.with_columns(
            best=pl.max_horizontal("b", "s"),
            cap=(
                pl.when(pl.col("b") > 0).then(pl.col("b") * pl.col("ask_size")).otherwise(0.0)
                + pl.when(pl.col("s") > 0).then(pl.col("s") * pl.col("bid_size")).otherwise(0.0)
            )
            * pl.col("payout_scale"),  # edges are per $1 of payout; scale to $ per contract
            cap1=(
                pl.when(pl.col("b") > 0.01 + EPS)
                .then(pl.col("b") * pl.col("ask_size"))
                .otherwise(0.0)
                + pl.when(pl.col("s") > 0.01 + EPS)
                .then(pl.col("s") * pl.col("bid_size"))
                .otherwise(0.0)
            )
            * pl.col("payout_scale"),
            cap2=(
                pl.when(pl.col("b") > 0.02 + EPS)
                .then(pl.col("b") * pl.col("ask_size"))
                .otherwise(0.0)
                + pl.when(pl.col("s") > 0.02 + EPS)
                .then(pl.col("s") * pl.col("bid_size"))
                .otherwise(0.0)
            )
            * pl.col("payout_scale"),
        )
        per = x.group_by("product", "snapshot_id").agg(
            best=pl.col("best").max(), cap=pl.col("cap").sum(),
            cap1=pl.col("cap1").sum(), cap2=pl.col("cap2").sum()
        ).filter(pl.col("best").is_not_null())  # fmt: skip
        for (product,), g in per.group_by("product"):
            rows.append({
                "product": product, "fee": f, "n_snapshots": g.height,
                "pct_snapshots_best_edge_gt_1c": _f((g["best"] > 0.01 + EPS).mean()),
                "pct_snapshots_best_edge_gt_2c": _f((g["best"] > 0.02 + EPS).mean()),
                "pct_snapshots_best_edge_le_0": _f((g["best"] <= EPS).mean()),
                "capturable_median": _f(g["cap"].median()),
                "capturable_p90": _f(g["cap"].quantile(0.9, "linear")),
                "capturable_gt_1c_median": _f(g["cap1"].median()),
                "capturable_gt_2c_median": _f(g["cap2"].median()),
            })  # fmt: skip
    schema = {
        "product": pl.String, "fee": pl.Float64, "n_snapshots": pl.Int64,
        "pct_snapshots_best_edge_gt_1c": pl.Float64, "pct_snapshots_best_edge_gt_2c": pl.Float64,
        "pct_snapshots_best_edge_le_0": pl.Float64, "capturable_median": pl.Float64,
        "capturable_p90": pl.Float64, "capturable_gt_1c_median": pl.Float64,
        "capturable_gt_2c_median": pl.Float64,
    }  # fmt: skip
    return pl.DataFrame(rows, schema=schema).sort("product", "fee")


def persistence(
    ef: pl.DataFrame,
    thr: float = 0.03,
    grid: pl.DataFrame | None = None,
    sim: str = "simultaneous",
    value: str = "mid_gap",
) -> pl.DataFrame:
    """Runs of consecutive snapshots with |gap| > thr, per (product, fix, strike).

    `grid` (product, fix_ts_ns, ts_ns: every collected snapshot, e.g. the ladder table) defines
    the snapshot ordinals within each (product, fix). Runs never cross sessions, and any snapshot
    missing from `ef` (dropped, filtered out as mismatched, no chain) breaks a run because its
    ordinal is skipped. Without `grid`, ordinals come from `ef` itself. A crossed row, a null gap
    or |gap| <= thr also breaks a run, as does a row outside the `sim` bucket. `sim`:
    "simultaneous" (mid_gap), "non-simultaneous" (`value`, normally "gap_outside") or "adjusted"
    (the verdict series: simultaneous rows use mid_gap, non-simultaneous rows use gap_outside, so a
    non-simultaneous snapshot is a hit when |gap_outside| > thr and breaks the run when
    gap_outside is null or <= thr; conservative because |gap_outside| <= |gap|). A run is attributed to the moneyness bucket of its first
    snapshot. Only pre-fix rows are used.
    """
    d = _adjusted(ef.filter(pl.col("ttf_bucket") != "post-fix"))
    src = grid if grid is not None else ef
    ordinal: dict[tuple[str, int, int], int] = {}
    for (prod, fix), g in src.group_by("product", "fix_ts_ns"):
        for i, t in enumerate(sorted(g["ts_ns"].unique().to_list())):
            ordinal[(str(prod), int(fix), int(t))] = i
    runs: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_key: dict[tuple[str, int, float], list[dict[str, Any]]] = defaultdict(list)
    for r in d.sort("ts_ns").iter_rows(named=True):
        by_key[(r["product"], r["fix_ts_ns"], r["strike"])].append(r)
    for (pname, fix, _k), rows in by_key.items():
        cur, first_bucket, prev = 0, "", -10
        for r in rows:
            o = ordinal[(pname, fix, r["ts_ns"])]
            hit = (
                (not r["crossed"])
                and (sim == "adjusted" or r["sim_bucket"] == sim)
                and r["adj" if sim == "adjusted" else value] is not None
                and abs(r["adj" if sim == "adjusted" else value]) > thr + EPS
            )
            if hit and cur and o == prev + 1:
                cur += 1
            else:
                if cur:
                    runs[(pname, first_bucket)].append(cur)
                cur = 0
                if hit:
                    cur, first_bucket = 1, r["m_bucket"]
            prev = o
        if cur:
            runs[(pname, first_bucket)].append(cur)
    out = []
    for (product, bucket), rl in sorted(runs.items()):
        sr = sorted(rl)
        mid = len(sr) // 2
        med = float(sr[mid]) if len(sr) % 2 else 0.5 * (sr[mid - 1] + sr[mid])
        out.append({"product": product, "m_bucket": bucket, "n_runs": len(sr),
                    "median_run": med, "max_run": max(sr)})  # fmt: skip
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
    """Rows with a positive model-free edge on the DELTA-WIDENED band (arb_*_edge_w; w = 0 when
    the future did not move). The arb_buy_edge / arb_sell_edge columns hold the widened edges.
    opt_rel_width = widest (ask-bid)/mid among the (up to
    four) option strikes nearest the venue strike. opt_ts_lag_ms = |ladder fetch time - option
    fetch time| (information; a non-zero lag is expected live and is covered by the delta widening).
    suspect if width > 0.5, lag > 60 s (ARB_MAX_LAG_S), w is null (move or sigma unknown), or the row is
    post-fix or on a stale (carried) chain. arb_usd = positive widened edge x displayed
    taker-side size x payout_scale. Rows are labelled (post_fix, sim_bucket), not dropped."""
    d = ef.filter(
        ~pl.col("crossed") & ((pl.col("arb_buy_edge_w") > 0) | (pl.col("arb_sell_edge_w") > 0))
    )
    chains = _opt_widths(tables["options"])
    rows = []
    for r in d.iter_rows(named=True):
        q = chains.get((r["opt_snapshot_id"], r["opt_expiry_ts_ns"]), [])
        below = [x for x in q if x[0] <= r["strike"]][-2:]
        above = [x for x in q if x[0] >= r["strike"]][:2]
        w = [(a - b) / max(0.5 * (a + b), 1e-9) for _k, b, a in below + above]
        rel = max(w) if w else float("inf")
        lag = abs(r["ladder_ts_ns"] - r["opt_ts_ns"]) / 1e6
        post = r["ttf_bucket"] == "post-fix"
        buy, sell = r["arb_buy_edge_w"], r["arb_sell_edge_w"]
        usd = (max(buy or 0.0, 0.0) * r["ask_size"] + max(sell or 0.0, 0.0) * r["bid_size"]) * r[
            "payout_scale"
        ]
        rows.append({
            "product": r["product"], "snapshot_id": r["snapshot_id"], "ts_ns": r["ts_ns"],
            "strike": r["strike"], "yes_bid": r["yes_bid"], "yes_ask": r["yes_ask"],
            "arb_buy_edge": buy, "arb_sell_edge": sell, "w": r["w"],
            "sim_bucket": r["sim_bucket"],
            "bid_size": r["bid_size"], "ask_size": r["ask_size"], "arb_usd": usd,
            "post_fix": post, "df_flag": r["df_flag"],
            "opt_ts_lag_ms": lag, "opt_rel_width": rel,
            "suspect": rel > SUSPECT_REL_WIDTH or lag > ARB_MAX_LAG_S * 1000 or post
            or r["w"] is None or bool(r["stale_chain"]),
        })  # fmt: skip
    schema = {
        "product": pl.String, "snapshot_id": pl.String, "ts_ns": pl.Int64, "strike": pl.Float64,
        "yes_bid": pl.Float64, "yes_ask": pl.Float64, "arb_buy_edge": pl.Float64,
        "arb_sell_edge": pl.Float64, "w": pl.Float64, "sim_bucket": pl.String,
        "bid_size": pl.Int64, "ask_size": pl.Int64, "arb_usd": pl.Float64, "post_fix": pl.Boolean, "df_flag": pl.Boolean,
        "opt_ts_lag_ms": pl.Float64, "opt_rel_width": pl.Float64, "suspect": pl.Boolean,
    }  # fmt: skip
    return pl.DataFrame(rows, schema=schema)


def _volume_steps(lad: pl.DataFrame) -> pl.DataFrame:
    """Positive volume steps per (product, fix_ts_ns, strike) and book, i.e. detected trades.

    Trades are only visible as volume increases. Both tickers are read: YES/Call (`volume`,
    `last`) and NO/Put (`raw_no_volume`, `1 - raw_no_last` as the YES-equivalent price). Volume is
    a size, so a null reading is 0 (it only means "nothing reported yet") and the first row of a
    series is the baseline, not a step. For ForecastEx it is [?] whether a matched YES/NO
    execution prints on both tickers (venue_facts), so the sum may double count.
    Columns: product, fix_ts_ns, strike, ts_ns, dv, px, book.
    """
    frames = []
    for book, vcol, pexpr in (
        ("yes", "volume", pl.col("last")),
        ("no", "raw_no_volume", 1.0 - pl.col("raw_no_last")),
    ):
        if vcol not in lad.columns:
            continue
        d = lad.sort("product", "fix_ts_ns", "strike", "ts_ns").with_columns(
            v=pl.col(vcol).fill_null(0.0), px=pexpr
        )
        d = d.with_columns(
            dv=pl.col("v") - pl.col("v").shift(1).over("product", "fix_ts_ns", "strike")
        )
        frames.append(
            d.filter(pl.col("dv") > 0).select(
                "product", "fix_ts_ns", "strike", "ts_ns", "dv", "px", pl.lit(book).alias("book")
            )
        )
    if not frames:
        return pl.DataFrame(schema={"product": pl.String, "fix_ts_ns": pl.Int64,
                                    "strike": pl.Float64, "ts_ns": pl.Int64, "dv": pl.Float64,
                                    "px": pl.Float64, "book": pl.String})  # fmt: skip
    return pl.concat(frames)


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


def _f_fix(tables: dict[str, pl.DataFrame]) -> dict[tuple[str, int], float]:
    """Mid of the first post-fix futures row per (product, fix_ts_ns)."""
    fut, lad = tables["futures"], tables["ladder"]
    if fut.height == 0:
        return {}
    fix_of = lad.group_by("snapshot_id").agg(fix=pl.col("fix_ts_ns").first())
    d = fut.join(fix_of, on="snapshot_id", how="inner").filter(pl.col("ts_ns") >= pl.col("fix"))
    out: dict[tuple[str, int], float] = {}
    for r in d.sort("ts_ns").iter_rows(named=True):
        key = (r["product"], r["fix"])
        if key in out:
            continue
        if r["bid"] is not None and r["ask"] is not None:
            out[key] = 0.5 * (r["bid"] + r["ask"])
        elif r.get("last") is not None:
            out[key] = r["last"]
    return out


def post_fix_activity(
    tables: dict[str, pl.DataFrame], ef: pl.DataFrame | None = None
) -> pl.DataFrame:
    """Per product: ladder rows with a resting quote after the fix, trades after the fix (volume
    steps on both tickers; trades are only visible as volume increases), their price vs the last
    pre-fix fair at that (fix, strike) (needs `ef`) and vs the settled outcome 1{F_fix > K}, with
    F_fix the mid of the first post-fix futures row of that fix."""
    lad = tables["ladder"]
    post = lad.filter(pl.col("ts_ns") >= pl.col("fix_ts_ns"))
    resting = post.filter(
        pl.col("yes_bid").is_not_null() | pl.col("yes_ask").is_not_null()
    ).group_by("product").agg(quotes_resting=pl.len())  # fmt: skip
    steps = _volume_steps(lad).filter(pl.col("ts_ns") >= pl.col("fix_ts_ns"))
    if ef is not None and ef.height:
        pre = (
            _headline(ef, None)
            .sort("ts_ns")
            .group_by("product", "fix_ts_ns", "strike", maintain_order=True)
            .agg(pre_fair=pl.col("fair").last())
        )
        steps = steps.join(pre, on=["product", "fix_ts_ns", "strike"], how="left")
    else:
        steps = steps.with_columns(pre_fair=pl.lit(None, dtype=pl.Float64))
    f_fix = _f_fix(tables)
    steps = steps.with_columns(
        px_vs_fair=pl.col("px") - pl.col("pre_fair"),
        outcome=pl.struct("product", "fix_ts_ns", "strike").map_elements(
            lambda r: (
                None
                if (r["product"], r["fix_ts_ns"]) not in f_fix
                else float(f_fix[(r["product"], r["fix_ts_ns"])] > r["strike"])
            ),
            return_dtype=pl.Float64,
        ),
    ).with_columns(px_vs_outcome=pl.col("px") - pl.col("outcome"))
    tr = steps.group_by("product").agg(
        trades_after_fix=pl.len(), volume_after_fix=pl.col("dv").sum(),
        median_trade_px_vs_fair=pl.col("px_vs_fair").median(),
        median_trade_px_vs_outcome=pl.col("px_vs_outcome").median(),
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


def arb_totals(arb: pl.DataFrame) -> pl.DataFrame:
    """Per product x suspect: rows, snapshots and total arb $ (edge x size x payout_scale)."""
    return (
        arb.group_by("product", "suspect")
        .agg(
            rows=pl.len(),
            snapshots=pl.col("snapshot_id").n_unique(),
            arb_usd=pl.col("arb_usd").sum(),
        )
        .sort("product", "suspect")
    )


def mismatch_tilt(ef: pl.DataFrame) -> pl.DataFrame:
    """Bias direction per chain role: median of gap x sign(F - K), i.e. venue mid minus fair
    signed so that a positive value means the venue is rich on the ITM digital / cheap on the OTM
    one relative to that chain. Compare roles: a chain from a later expiry ("above") and an
    earlier one ("below") should tilt opposite ways if the mismatch drives the gap."""
    d = _headline(ef).filter(pl.col("mid_gap").is_not_null() & (pl.col("F") != pl.col("strike")))
    d = d.with_columns(
        tilt=pl.col("mid_gap") * pl.when(pl.col("F") > pl.col("strike")).then(1.0).otherwise(-1.0)
    )
    return (
        d.group_by("product", "expiry_role")
        .agg(n=pl.len(), median_tilt=pl.col("tilt").median())
        .sort("product", "expiry_role")
    )
