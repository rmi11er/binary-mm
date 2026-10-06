"""Quote-quality metrics (spec 60, iteration 4): stale quotes vs a venue reference, touch requote
cadence and D churn. Pure functions over stored snapshots / burst samples; no network, no orders."""

from __future__ import annotations

import math
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


def order_fee(coeff: float, price: float, size: float) -> float:
    """Fee on a whole order, rounded UP to the cent (venues round per order; this flatters nobody)."""
    return math.ceil(coeff * size * price * (1.0 - price) * 100 - 1e-9) / 100


def stale_stats(points: list[dict[str, Any]], tick: float = TICK) -> dict[str, Any] | None:
    """Stale-quote statistics from consecutive snapshots of ONE market with a reference fair value.

    A snapshot is stale when best bid > ref + fee + tick (we could sell into it) or best ask <
    ref - fee - tick (we could buy). Unscorable points (no ref_fair / fee_coeff / no book, or both
    sides stale = a crossed book vs the reference, treated as bad data) stay in the time sequence
    as non-stale gaps but are not counted. An episode is a run of consecutive stale snapshots, broken
    by an unscored point or a time gap > 1.5x the median spacing; its duration is last - first plus one
    spacing. takeable_day is FIRST-TOUCH takeable: the displayed touch size at the episode's first
    stale snapshot, net of the order fee rounded up to the cent, once per episode, over the observed
    span. It ignores latency/competition for the size (optimistic) and deeper levels/refills
    (pessimistic), so it is neither a bound nor a forecast.
    """
    pts = sorted(points, key=lambda p: p["ts_ns"])
    if not pts:
        return None
    flags: list[bool] = []
    scored: list[bool] = []
    takeable: list[float] = []
    for p in pts:
        ref, c, bid, ask = p.get("ref_fair"), p.get("fee_coeff"), p.get("bid"), p.get("ask")
        if ref is None or c is None or (bid is None and ask is None):
            flags.append(False), scored.append(False), takeable.append(0.0)
            continue
        sb = bid is not None and bid > ref + fee(c, bid) + tick
        sa = ask is not None and ask < ref - fee(c, ask) - tick
        if (
            sb and sa
        ):  # both sides stale: bid > ask vs the reference -> bad data, not an opportunity
            flags.append(False), scored.append(False), takeable.append(0.0)
            continue
        edge = 0.0
        if sb:
            size = p.get("bid_size") or 0.0
            edge = max(0.0, (bid - ref) * size - order_fee(c, bid, size))
        if sa:
            size = p.get("ask_size") or 0.0
            edge = max(0.0, (ref - ask) * size - order_fee(c, ask, size))
        flags.append(sb or sa), scored.append(True), takeable.append(edge)
    n_scored = sum(scored)
    if n_scored == 0:
        return None
    ts = [p["ts_ns"] for p in pts]
    gaps = [b - a for a, b in pairwise(ts)]
    spacing = statistics.median(gaps) if gaps else 0
    durs: list[float] = []
    first_edges: list[float] = []
    censored = 0
    i = 0
    while i < len(pts):
        if flags[i]:
            j = i
            while j + 1 < len(pts) and flags[j + 1] and ts[j + 1] - ts[j] <= 1.5 * spacing:
                j += 1
            durs.append((ts[j] - ts[i] + spacing) / 1e9)
            first_edges.append(takeable[i])
            censored += int(i == 0 or j == len(pts) - 1)
            i = j + 1
        else:
            i += 1
    span = ts[-1] - ts[0] + spacing
    return {
        "n_scored": n_scored,
        "n_stale": sum(flags),
        "stale_pct": sum(flags) / n_scored,
        "episodes": len(durs),
        "censored_runs": censored,  # runs touching the first/last snapshot: duration is a lower bound
        "median_run_s": statistics.median(durs) if durs else None,
        "takeable_day": sum(first_edges) / (span / NS_DAY) if span > 0 else None,
    }


def touch_changes(samples: list[dict[str, Any]]) -> list[float]:
    """Seconds between consecutive touch PRICE changes (best bid or ask) in high-frequency samples.
    Size-only changes are ignored: other traders' fills and adds would split one periodic requote.
    Resolution is the sampling interval; same-price requotes are invisible."""
    s = sorted(samples, key=lambda x: x["ts_ns"])
    change_ts = [
        b["ts_ns"]
        for a, b in pairwise(s)
        if (a.get("bid"), a.get("ask")) != (b.get("bid"), b.get("ask"))
    ]
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
    multiples: tuple[int, ...] = (1, 2, 3),
) -> dict[int, dict[str, Any]]:
    """Flag requote clustering at a period. Only periods >= 3 sampling intervals are testable (below
    that the smallest observable interval falls inside the window). An interval counts if it is within
    max(sample_dt, tol_frac x period) of k x period for k in `multiples` (a quoter that moves the touch
    every 2nd/3rd requote). The flag needs n >= min_n, a share >= `share` AND at least twice the share
    an exponential (memoryless) requote process with the same mean interval would put in those windows."""
    mean = statistics.fmean(intervals) if intervals else 0.0
    out: dict[int, dict[str, Any]] = {}
    for p in periods:
        if p < 3 * sample_dt:
            out[p] = {
                "flag": False,
                "share": 0.0,
                "expected": 0.0,
                "n": len(intervals),
                "testable": False,
            }
            continue
        tol = max(sample_dt, tol_frac * p)
        near = sum(1 for v in intervals if any(abs(v - k * p) <= tol for k in multiples))
        sh = near / len(intervals) if intervals else 0.0
        exp_ = (
            sum(
                math.exp(-max(0.0, k * p - tol) / mean) - math.exp(-(k * p + tol) / mean)
                for k in multiples
            )
            if mean > 0
            else 0.0
        )
        out[p] = {"flag": len(intervals) >= min_n and sh >= share and sh >= 2 * exp_, "share": sh,
                  "expected": exp_, "n": len(intervals), "testable": True}  # fmt: skip
    return out


def d_churn(series: list[float], min_n: int = 5) -> float | None:
    """Coefficient of variation (sample std / mean) of D across snapshots."""
    if len(series) < min_n:
        return None
    mean = statistics.fmean(series)
    return None if mean == 0 else statistics.stdev(series) / mean
