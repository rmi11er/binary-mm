"""Owner decision rule for the spot check (docs/spotcheck_spec.md "Decision rule").

Owner reading: KILL, then GO, then INVESTIGATE. GO is evaluated on expiry-matched data only;
the taker clause of KILL is evaluated at VERDICT_FEE. All statistics passed in are pre-fix and
exclude crossed rows (see metrics.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import polars as pl

from bmm.spotcheck.metrics import MONEYNESS_BUCKETS

# OWNER-OWNED (docs/spotcheck_spec.md "Decision rule"). Agents must not change these values.
GO_MEDIAN_ABS_GAP = 0.03
GO_PCT_GT_3C = 0.30
GO_MIN_PERSISTENCE = 3
GO_MIN_MEDIAN_SIZE = 20
KILL_MEDIAN_ABS_GAP = 0.02
KILL_PCT_TAKER_LE_0 = 0.90
VERDICT_FEE = 0.02
TOL = 1e-9


@dataclass(frozen=True)
class Verdict:
    label: Literal["GO", "INVESTIGATE", "KILL"]
    reasons: list[str]
    numbers: dict[str, float]


@dataclass
class _Bucket:
    gap: float
    pct: float
    run: float | None
    size: float | None


def _buckets(
    gaps: pl.DataFrame, pers: pl.DataFrame, sizes: pl.DataFrame, product: str, scope: str
) -> dict[str, _Bucket]:
    def pick(df: pl.DataFrame, col: str) -> dict[str, float]:
        if df.height == 0:
            return {}
        d = df.filter((pl.col("product") == product) & (pl.col("scope") == scope))
        return {r["m_bucket"]: r[col] for r in d.iter_rows(named=True)}

    run, size = pick(pers, "median_run"), pick(sizes, "median_size")
    g = gaps.filter((pl.col("product") == product) & pl.col("m_bucket").is_in(MONEYNESS_BUCKETS))
    return {
        r["m_bucket"]: _Bucket(r["median_abs_gap"], r["pct_gt_3c"], run.get(r["m_bucket"]),
                               size.get(r["m_bucket"]))
        for r in g.iter_rows(named=True)
    }  # fmt: skip


def _gap_ok(b: _Bucket) -> bool:
    return b.gap >= GO_MEDIAN_ABS_GAP - TOL and b.pct >= GO_PCT_GT_3C - TOL


def _run_ok(b: _Bucket) -> bool:
    return b.run is not None and b.run >= GO_MIN_PERSISTENCE - TOL


def _size_ok(b: _Bucket) -> bool:
    return b.size is not None and b.size >= GO_MIN_MEDIAN_SIZE - TOL


def verdict(
    product: str,
    gap_all: pl.DataFrame,
    gap_matched_only: pl.DataFrame,
    persistence: pl.DataFrame,
    sizes: pl.DataFrame,
    taker: pl.DataFrame,
) -> Verdict:
    """`persistence` and `sizes` carry a `scope` column ("matched" or "all"); `taker` has one row
    per (product, fee). Gap frames are metrics.gap_stats output."""
    allb = _buckets(gap_all, persistence, sizes, product, "all")
    matched = _buckets(gap_matched_only, persistence, sizes, product, "matched")
    t = taker.filter((pl.col("product") == product) & (pl.col("fee") - VERDICT_FEE).abs().lt(1e-9))
    le0 = float(t["pct_snapshots_best_edge_le_0"][0]) if t.height else None

    numbers: dict[str, float] = {}
    for scope, bs in (("all", allb), ("matched", matched)):
        for name, b in sorted(bs.items()):
            numbers[f"{scope}.{name}.median_abs_gap"] = b.gap
            numbers[f"{scope}.{name}.pct_gt_3c"] = b.pct
            if b.run is not None:
                numbers[f"{scope}.{name}.median_run"] = b.run
            if b.size is not None:
                numbers[f"{scope}.{name}.median_size"] = b.size
    if le0 is not None:
        numbers["pct_snapshots_best_edge_le_0"] = le0
    if allb:
        numbers["max_bucket_median_abs_gap"] = max(b.gap for b in allb.values())

    if not allb:
        return Verdict("INVESTIGATE", ["no data: no usable pre-fix rows for this product"],
                       numbers)  # fmt: skip

    # 1. KILL
    kill: list[str] = []
    top = max(b.gap for b in allb.values())
    if top < KILL_MEDIAN_ABS_GAP - TOL:
        kill.append(
            f"median |gap| < {KILL_MEDIAN_ABS_GAP * 100:g}c in every bucket "
            f"(largest {top * 100:.2f}c)"
        )
    if le0 is not None and le0 > KILL_PCT_TAKER_LE_0 + TOL:
        kill.append(
            f"taker edge after fees <= 0 in {le0:.0%} of snapshots at fee {VERDICT_FEE:g} "
            f"(> {KILL_PCT_TAKER_LE_0:.0%})"
        )
    if kill:
        return Verdict("KILL", kill, numbers)

    # 2. GO, matched-only data
    for name, b in sorted(matched.items()):
        if _gap_ok(b) and _run_ok(b) and _size_ok(b):
            return Verdict(
                "GO",
                [
                    (
                        f"bucket {name} (expiry-matched): median |gap| {b.gap * 100:.2f}c, "
                        f"{b.pct:.0%} of observations > 3c, median run {b.run:g} snapshots, "
                        f"median size {b.size:g}"
                    )
                ],
                numbers,
            )  # fmt: skip

    # 3. INVESTIGATE: say why GO did not hold
    reasons: list[str] = []
    for name, b in sorted(allb.items()):
        if _gap_ok(b) and _run_ok(b) and _size_ok(b):
            m = matched.get(name)
            if m is None or not (_gap_ok(m) and _run_ok(m) and _size_ok(m)):
                reasons.append(
                    f"bucket {name}: GO conditions hold only with expiry-mismatched data "
                    "(matched-only data does not qualify)"
                )
    for name, b in sorted(matched.items()):
        if not _gap_ok(b):
            continue
        if not _size_ok(b):
            reasons.append(
                f"bucket {name}: gaps qualify but displayed size is below "
                f"{GO_MIN_MEDIAN_SIZE} (median {b.size if b.size is not None else 'n/a'})"
            )
        if not _run_ok(b):
            reasons.append(
                f"bucket {name}: gaps without persistence (median run "
                f"{b.run if b.run is not None else 'n/a'} < {GO_MIN_PERSISTENCE} snapshots)"
            )
    if not reasons:
        reasons.append("gaps exist but no bucket reaches the GO thresholds and KILL does not apply")
    return Verdict("INVESTIGATE", reasons, numbers)
