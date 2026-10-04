"""Cost model, markout shrinkage and collateral allocation over half-markets (spec 60).

All costs are $/hr per posted contract. `reward(s)` is the venue-scored reward in $/hr for
posting s contracts on one side (bound from the adapter's `expected_reward`).
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

HOURS_YR = 8760.0
COLL_RATE = 0.05  # opportunity cost of collateral per year
N0 = 20  # trades needed before the market's own markout gets full weight
EPS = 1e-9


@dataclass(frozen=True)
class Half:
    key: str
    market: str
    venue: str
    two_sided: bool  # program only pays when both sides qualify -> allocate q on both together
    reward: Callable[[int], float]
    c: float  # cost $/hr per posted contract (see cost_hr)
    coll: float  # collateral $/contract on this side
    q: int  # qualifying minimum contracts
    s_star_cf: float | None = None  # closed-form optimum where the venue formula allows it
    c_lower_bound: bool = False


@dataclass(frozen=True)
class Alloc:
    key: str
    s: int
    reward_day: float
    cost_day: float
    collateral: float
    roc_day: float
    c_lower_bound: bool


def shrink_markout(
    m_mkt: float | None, n: int, m_cat: float | None, n0: int = N0
) -> tuple[float, bool]:
    """Shrink the market markout toward the category mean. Returns (markout, lower_bound_flag)."""
    if m_cat is None:
        return (m_mkt, False) if m_mkt is not None else (0.0, True)
    if m_mkt is None or n <= 0:
        return m_cat, False
    w = min(n, n0) / n0
    return w * m_mkt + (1 - w) * m_cat, False


def cost_hr(
    fills_side_hr: float,
    depth_raw_side: float,
    m: float,
    maker_fee: float,
    maker_rebate: float,
    coll: float,
) -> float:
    """f x cost_per_fill + collateral carry. m is the signed maker markout (negative = adverse)."""
    f = fills_side_hr / max(depth_raw_side, 1.0)
    per_fill = max(0.0, max(0.0, -m) - maker_rebate + maker_fee)
    return f * per_fill + coll * COLL_RATE / HOURS_YR


def ev(h: Half, s: int) -> float:
    return h.reward(s) - h.c * s if s > 0 else 0.0


def _argmax_int(f: Callable[[int], float], lo: int, hi: int) -> int:
    """Integer argmax of a unimodal f on [lo, hi] by ternary search."""
    while hi - lo > 2:
        m1, m2 = lo + (hi - lo) // 3, hi - (hi - lo) // 3
        if f(m1) < f(m2):
            lo = m1 + 1
        else:
            hi = m2
    return max(range(lo, hi + 1), key=f)


def s_star(h: Half, s_cap: int) -> int:
    if h.q > s_cap:
        return 0
    if h.s_star_cf is not None and math.isfinite(h.s_star_cf):
        lo = max(0, min(s_cap, math.floor(h.s_star_cf)))
        s = max((lo, min(s_cap, lo + 1)), key=lambda x: ev(h, x))
    else:
        s = _argmax_int(lambda x: ev(h, x), h.q, s_cap)
    s = max(s, h.q)  # below-threshold optimum is raised to q ...
    return s if ev(h, s) > 0 else 0  # ... unless 0 has higher EV


def _alloc(h: Half, n: int) -> Alloc:
    rd = 24 * h.reward(n) if n else 0.0
    cd = 24 * h.c * n
    col = n * h.coll
    return Alloc(h.key, n, rd, cd, col, (rd - cd) / col if col else 0.0, h.c_lower_bound)


def allocate(
    halves: list[Half], budget: float = 8000.0, mkt_cap: float = 0.15, venue_cap: float = 0.40
) -> list[Alloc]:
    """Greedy by marginal EV per collateral $. Qualifying blocks go in whole (both sides together
    for two-sided programs). Heuristic when q > 1; exact for unit blocks with concave rewards."""
    by = {h.key: h for h in halves}
    s = {h.key: 0 for h in halves}
    groups: dict[str, list[Half]] = {}
    for h in halves:
        groups.setdefault(h.market if h.two_sided else h.key, []).append(h)
    smax: dict[str, int] = {}
    for h in halves:
        st = s_star(h, int(mkt_cap * budget // h.coll))
        smax[h.key] = max(st, h.q) if h.two_sided else st
    used = 0.0
    vused: defaultdict[str, float] = defaultdict(float)
    mused: defaultdict[str, float] = defaultdict(float)
    heap: list[tuple[float, int, str, str | None]] = []
    seq = itertools.count()
    for g, hs in groups.items():
        if not hs[0].two_sided and smax[hs[0].key] == 0:
            continue
        bev = sum(ev(h, h.q) for h in hs)
        bcoll = sum(h.q * h.coll for h in hs)
        if bev > 0:
            heapq.heappush(heap, (-bev / bcoll, next(seq), g, None))
    while heap:
        negr, _, g, hk = heapq.heappop(heap)
        if -negr <= 0:
            break
        hs = groups[g]
        mkt, v = hs[0].market, hs[0].venue
        add = {h.key: h.q for h in hs} if hk is None else {hk: 1}
        if hk is not None and s[hk] + 1 > smax[hk]:
            continue
        dc = sum(n * by[k].coll for k, n in add.items())
        if (
            used + dc > budget + EPS
            or vused[v] + dc > venue_cap * budget + EPS
            or mused[mkt] + dc > mkt_cap * budget + EPS
        ):
            continue
        for k, n in add.items():
            s[k] += n
        used += dc
        vused[v] += dc
        mused[mkt] += dc
        for k in add:
            h = by[k]
            if s[k] < smax[k]:
                gain = (ev(h, s[k] + 1) - ev(h, s[k])) / h.coll
                heapq.heappush(heap, (-gain, next(seq), g, k))
    return [_alloc(h, s[h.key]) for h in halves]
