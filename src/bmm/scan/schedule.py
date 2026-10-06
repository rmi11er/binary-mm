"""Reward schedule maths: dollars come from the program's actual time windows, never rate x 24.

A schedule is a JSON list of [start_ns | null, end_ns | null, usd_per_hr] segments (null start =
already running, null end = open-ended and assumed to continue, one pool per day).
"""

from __future__ import annotations

import json

from bmm.scan.models import Program

H_NS = 3600 * 10**9
HORIZON_NS = 24 * H_NS


def make_schedule(segs: list[tuple[int | None, int | None, float]]) -> str:
    return json.dumps([[s, e, r] for s, e, r in segs])


def segments(prog: Program) -> list[tuple[int | None, int | None, float]]:
    return [(s, e, float(r)) for s, e, r in json.loads(prog.schedule or "[]")]


def reward_hours(prog: Program, now_ns: int, horizon_ns: int = HORIZON_NS) -> float:
    """Effective hours of reward at the program's current rate over the next `horizon_ns`: the
    integral of rate(t)/R_now over [now, now+horizon] intersected with the schedule. A program
    with no schedule is legacy and counts as a full 24h (the caller flags it)."""
    segs = segments(prog)
    if not segs:
        return 24.0
    if prog.R_usd_hr <= 0:
        return 0.0
    end_h = now_ns + horizon_ns
    total = 0.0
    for s, e, r in segs:
        lo = max(now_ns, s if s is not None else now_ns)
        hi = min(end_h, e if e is not None else end_h)
        if hi > lo:
            total += (hi - lo) / H_NS * r / prog.R_usd_hr
    return total


def pool_remaining(prog: Program, now_ns: int) -> float:
    """USD still to be paid by the schedule from now: rate x time left per segment; an open-ended
    segment contributes one day's pool. No schedule: one day at the current rate."""
    segs = segments(prog)
    if not segs:
        return prog.R_usd_hr * 24
    total = 0.0
    for s, e, r in segs:
        lo = max(now_ns, s if s is not None else now_ns)
        if e is None:
            total += r * 24
        elif e > lo:
            total += r * (e - lo) / H_NS
    return total


def is_open_ended(prog: Program) -> bool:
    return any(e is None for _, e, _ in segments(prog))
