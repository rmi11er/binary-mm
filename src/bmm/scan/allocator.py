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
    stress_per: float = 0.0  # adverse jump $/contract on this side (see metrics.adverse_jump)


@dataclass(frozen=True)
class Alloc:
    key: str
    s: int
    reward_day: float
    cost_day: float
    collateral: float
    roc_day: float
    c_lower_bound: bool
    stress_loss: float = 0.0  # posted size x adverse jump, $


def shrink_markout(
    m_mkt: float | None, n: int, m_cat: float | None, n0: int = N0
) -> tuple[float, bool]:
    """Shrink the market markout toward a prior. Returns (markout, lower_bound_flag).

    The prior is the category mean; the caller falls back to the all-category mean first. With
    no prior at all it is 0 (no adverse selection), so the result is only a cost lower bound
    and is flagged whenever that prior carries weight.
    """
    w = 0.0 if m_mkt is None or n <= 0 else min(n, n0) / n0
    prior = 0.0 if m_cat is None else m_cat
    m = prior if m_mkt is None else w * m_mkt + (1 - w) * prior
    return m, (m_cat is None and w < 1)


def cost_hr(
    fills_side_hr: float,
    depth_raw_side: float,
    m: float,
    maker_fee: float,
    maker_rebate: float,
    coll: float,
) -> float:
    """f x cost_per_fill + collateral carry. m is the signed maker markout (negative = adverse).

    fills_side_hr counts contracts (not trades); depth_raw_side is the resting depth the fills
    are shared with (queue-share approximation, our own size excluded, so f is overstated).
    """
    f = fills_side_hr / max(depth_raw_side, 1.0)
    per_fill = max(0.0, max(0.0, -m) - maker_rebate + maker_fee)
    return f * per_fill + coll * COLL_RATE / HOURS_YR


def ev(h: Half, s: int) -> float:
    return h.reward(s) - h.c * s if s > 0 else 0.0


def _argmax_int(f: Callable[[int], float], lo: int, hi: int) -> int:
    """Integer argmax of f on [lo, hi] by ternary search. Requires f strictly concave."""
    while hi - lo > 2:
        m1, m2 = lo + (hi - lo) // 3, hi - (hi - lo) // 3
        if f(m1) < f(m2):
            lo = m1 + 1
        else:
            hi = m2
    return max(range(lo, hi + 1), key=f)


def s_star(h: Half, s_cap: int) -> int:
    """Best size for one half-market. `reward` must be concave with reward(0) = 0."""
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
    return Alloc(
        h.key, n, rd, cd, col, (rd - cd) / col if col else 0.0, h.c_lower_bound, n * h.stress_per
    )


def _group_of(h: Half) -> str:
    return h.market if h.two_sided else h.key


@dataclass(frozen=True)
class Caps:
    budget: float
    mkt: float
    venue: float
    stress_mkt: float  # fraction of bankroll lost per market in the stress scenario
    stress_total: float
    bankroll: float


def allocate(
    halves: list[Half],
    budget: float = 8000.0,
    mkt_cap: float = 0.15,
    venue_cap: float = 0.40,
    stress_mkt_cap: float = 0.02,
    stress_total_cap: float = 0.10,
    bankroll: float | None = None,
) -> list[Alloc]:
    """Greedy plus one improvement pass: for each funded group, drop it, refill greedily and keep
    the best total EV. Exact for unit blocks with concave rewards; a heuristic when q > 1.

    Stress: every market must keep sum(size x adverse jump) <= stress_mkt_cap x bankroll and the
    book <= stress_total_cap x bankroll, on top of the collateral caps."""
    caps = Caps(budget, mkt_cap, venue_cap, stress_mkt_cap, stress_total_cap,
                budget if bankroll is None else bankroll)  # fmt: skip
    best = _greedy(halves, caps)

    def total(al: list[Alloc]) -> float:
        return sum(a.reward_day - a.cost_day for a in al)

    for g in sorted({_group_of(h) for h, a in zip(halves, best, strict=True) if a.s > 0}):
        trial = _greedy([h for h in halves if _group_of(h) != g], caps)
        if total(trial) > total(best) + EPS:
            by_key = {a.key: a for a in trial}
            best = [by_key.get(h.key, _alloc(h, 0)) for h in halves]
    return best


def _greedy(halves: list[Half], caps: Caps) -> list[Alloc]:
    """Greedy by marginal EV per collateral $. Qualifying blocks go in whole (both sides together
    for two-sided programs, which are skipped unless both halves are present)."""
    by = {h.key: h for h in halves}
    s = {h.key: 0 for h in halves}
    groups: dict[str, list[Half]] = {}
    for h in halves:
        groups.setdefault(_group_of(h), []).append(h)
    smax: dict[str, int] = {}
    for h in halves:
        cap = math.floor(caps.mkt * caps.budget / h.coll + 1e-9)
        if h.stress_per > 0:
            cap = min(cap, math.floor(caps.stress_mkt * caps.bankroll / h.stress_per + 1e-9))
        st = s_star(h, cap)
        smax[h.key] = max(st, h.q) if h.two_sided else st
    used = tstress = 0.0
    vused: defaultdict[str, float] = defaultdict(float)
    mused: defaultdict[str, float] = defaultdict(float)
    mstress: defaultdict[str, float] = defaultdict(float)
    heap: list[tuple[float, int, str, str | None]] = []
    seq = itertools.count()
    for g, hs in groups.items():
        if hs[0].two_sided and len(hs) < 2:
            continue
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
        ds = sum(n * by[k].stress_per for k, n in add.items())
        if (
            used + dc > caps.budget + EPS
            or vused[v] + dc > caps.venue * caps.budget + EPS
            or mused[mkt] + dc > caps.mkt * caps.budget + EPS
            or mstress[mkt] + ds > caps.stress_mkt * caps.bankroll + EPS
            or tstress + ds > caps.stress_total * caps.bankroll + EPS
        ):
            continue
        for k, n in add.items():
            s[k] += n
        used += dc
        tstress += ds
        vused[v] += dc
        mused[mkt] += dc
        mstress[mkt] += ds
        for k in add:
            h = by[k]
            if s[k] < smax[k]:
                gain = (ev(h, s[k] + 1) - ev(h, s[k])) / h.coll
                heapq.heappush(heap, (-gain, next(seq), g, k))
    return [_alloc(h, s[h.key]) for h in halves]
