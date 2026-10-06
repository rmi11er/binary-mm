"""Quote-quality metrics (spec 60, iteration 4): stale quotes vs a venue reference, touch requote
cadence and D churn. Pure functions over stored snapshots / burst samples; no network, no orders."""

from __future__ import annotations

import statistics
from itertools import pairwise
from typing import Any

TICK = 0.01
PERIODS = (5, 30, 60)  # seconds: requote periods to flag
_BINS = [(2, "<=2s"), (5, "2-5s"), (10, "5-10s"), (20, "10-20s"), (30, "20-30s"), (45, "30-45s"),
         (60, "45-60s"), (90, "60-90s"), (120, "90-120s")]  # fmt: skip
NS_DAY = 86400 * 10**9


def fee(coeff: float, price: float) -> float:
    """Taker fee per contract: coefficient x p(1-p)."""
    return coeff * price * (1.0 - price)


def stale_stats(points: list[dict[str, Any]], tick: float = TICK) -> dict[str, Any] | None:
    """Stale-quote statistics from consecutive snapshots of ONE market with a reference fair value.

    A snapshot is stale when best bid > ref + fee + tick (we could sell into it) or best ask <
    ref - fee - tick (we could buy). Points without ref_fair or fee_coeff are not scorable and are
    skipped. An episode is a run of consecutive stale snapshots; its duration is last - first plus
    one snapshot spacing (the run is only observed at the snapshots). takeable_day assumes ONE fill
    per episode, at the displayed touch size of its first stale snapshot, net of the taker fee,
    divided by the observed span; it is a lower bound on what repeated refills could take.
    """
    pts = sorted(
        (p for p in points if p.get("ref_fair") is not None and p.get("fee_coeff") is not None),
        key=lambda p: p["ts_ns"],
    )
    if not pts:
        return None
    flags: list[bool] = []
    takeable: list[float] = []
    for p in pts:
        ref, c = p["ref_fair"], p["fee_coeff"]
        edge, stale = 0.0, False
        bid, ask = p.get("bid"), p.get("ask")
        if bid is not None and bid > ref + fee(c, bid) + tick:
            stale, edge = True, edge + (bid - ref - fee(c, bid)) * (p.get("bid_size") or 0.0)
        if ask is not None and ask < ref - fee(c, ask) - tick:
            stale, edge = True, edge + (ref - ask - fee(c, ask)) * (p.get("ask_size") or 0.0)
        flags.append(stale)
        takeable.append(edge)
    ts = [p["ts_ns"] for p in pts]
    gaps = [b - a for a, b in pairwise(ts)]
    spacing = statistics.median(gaps) if gaps else 0
    durs: list[float] = []
    first_edges: list[float] = []
    i = 0
    while i < len(pts):
        if flags[i]:
            j = i
            while j + 1 < len(pts) and flags[j + 1]:
                j += 1
            durs.append((ts[j] - ts[i] + spacing) / 1e9)
            first_edges.append(takeable[i])
            i = j + 1
        else:
            i += 1
    span = ts[-1] - ts[0] + spacing
    return {
        "n_scored": len(pts),
        "n_stale": sum(flags),
        "stale_pct": sum(flags) / len(pts),
        "episodes": len(durs),
        "median_run_s": statistics.median(durs) if durs else None,
        "takeable_day": sum(first_edges) / (span / NS_DAY) if span > 0 else None,
    }


def touch_changes(samples: list[dict[str, Any]]) -> list[float]:
    """Seconds between consecutive touch changes (best bid/ask price or size) in high-frequency
    samples. Resolution is the sampling interval."""
    s = sorted(samples, key=lambda x: x["ts_ns"])
    key = lambda x: (x.get("bid"), x.get("ask"), x.get("bid_size"), x.get("ask_size"))
    change_ts = [b["ts_ns"] for a, b in pairwise(s) if key(a) != key(b)]
    return [(b - a) / 1e9 for a, b in pairwise(change_ts)]


def cadence_histogram(intervals: list[float]) -> dict[str, int]:
    h = {label: 0 for _, label in _BINS} | {">120s": 0}
    for v in intervals:
        h[next((lab for edge, lab in _BINS if v <= edge), ">120s")] += 1
    return h


def cadence_flags(
    intervals: list[float],
    sample_dt: float,
    periods: tuple[int, ...] = PERIODS,
    tol_frac: float = 0.1,
    share: float = 0.3,
    min_n: int = 8,
) -> dict[int, dict[str, Any]]:
    """Flag requote clustering: at least `share` of the intervals within +/-max(1.5 sampling
    intervals, tol_frac x period) of a period, with at least `min_n` intervals observed."""
    out: dict[int, dict[str, Any]] = {}
    for p in periods:
        tol = max(1.5 * sample_dt, tol_frac * p)
        near = sum(1 for v in intervals if abs(v - p) <= tol)
        sh = near / len(intervals) if intervals else 0.0
        out[p] = {"flag": len(intervals) >= min_n and sh >= share, "share": sh, "n": len(intervals)}
    return out


def d_churn(series: list[float], min_n: int = 5) -> float | None:
    """Coefficient of variation (sample std / mean) of D across snapshots."""
    if len(series) < min_n:
        return None
    mean = statistics.fmean(series)
    return None if mean == 0 else statistics.stdev(series) / mean
