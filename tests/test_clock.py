import time
from datetime import date

from bmm.clock import ct_to_ns, now_ns, ns_to_ct


def test_now_ns_is_utc_int():
    a = now_ns()
    b = time.time_ns()
    assert isinstance(a, int) and abs(b - a) < 1_000_000_000


def test_ct_to_ns_across_dst_end():
    # CDT (UTC-5) on 2026-10-30, CST (UTC-6) on 2026-11-02
    assert ct_to_ns(date(2026, 10, 30), 15, 0) == 1_793_390_400_000_000_000  # 20:00Z
    assert ct_to_ns(date(2026, 11, 2), 15, 0) == 1_793_653_200_000_000_000  # 21:00Z


def test_roundtrip():
    ns = ct_to_ns(date(2026, 11, 2), 12, 30)
    t = ns_to_ct(ns)
    assert (t.hour, t.minute, t.tzinfo.key) == (12, 30, "America/Chicago")  # type: ignore[union-attr]
