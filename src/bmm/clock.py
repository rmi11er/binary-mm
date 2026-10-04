"""Single clock for bmm: all timestamps are int ns UTC."""

from __future__ import annotations

import time
from datetime import date, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")


def now_ns() -> int:
    return time.time_ns()


def ct_to_ns(d: date, hh: int, mm: int, ss: int = 0) -> int:
    return int(datetime.combine(d, dtime(hh, mm, ss), tzinfo=CT).timestamp()) * 1_000_000_000


def ns_to_ct(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=CT)
