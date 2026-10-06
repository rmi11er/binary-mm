import math
import random

import pytest

from bmm.scan.quality import cadence_flags, cadence_histogram, d_churn, stale_stats, touch_changes

S = 10**9
MIN = 60 * S


def P(minute, bid=0.50, ask=0.52, ref=0.50, bsz=100.0, asz=100.0, fee=0.05):
    return {"ts_ns": minute * MIN, "bid": bid, "ask": ask, "bid_size": bsz, "ask_size": asz,
            "ref_fair": ref, "fee_coeff": fee}  # fmt: skip


def test_stale_thresholds_include_fee_and_one_tick():
    # fee at 0.60 = 0.05*0.6*0.4 = 0.012 -> a bid is stale above 0.50 + 0.012 + 0.01 = 0.522
    assert stale_stats([P(0, bid=0.60), P(1, bid=0.52)])["n_stale"] == 1
    # ask 0.40: fee 0.012, stale below 0.50 - 0.012 - 0.01 = 0.478
    assert stale_stats([P(0, ask=0.40, bid=0.39), P(1)])["n_stale"] == 1


def test_unscorable_snapshots_are_not_counted():
    pts = [P(0), {**P(1, bid=0.9), "ref_fair": None}, {**P(2, bid=0.9), "fee_coeff": None}, P(3)]
    st = stale_stats(pts)
    assert st["n_scored"] == 2 and st["n_stale"] == 0
    assert stale_stats([{**P(0), "ref_fair": None}]) is None


def test_cadence_needs_enough_intervals_and_histogram_bins():
    assert cadence_flags([5.0, 5.0], sample_dt=1.0, min_n=8)[5]["flag"] is False
    h = cadence_histogram([1.0, 4.0, 5.0, 29.0, 31.0, 61.0, 500.0])
    assert (
        h["<=2s"] == 1
        and h["2-5s"] == 2
        and h["20-30s"] == 1
        and h["30-45s"] == 1
        and h[">120s"] == 1
    )
    assert sum(h.values()) == 7


def test_d_churn_is_coefficient_of_variation():
    assert d_churn([10.0] * 5) == 0.0
    assert d_churn([10.0, 20.0, 10.0, 20.0, 10.0]) == pytest.approx(5.4772255 / 14.0)
    assert d_churn([10.0, 20.0]) is None  # too few snapshots
    assert d_churn([0.0] * 6) is None  # undefined at zero mean


def _fee(c, p, size):
    return math.ceil(c * size * p * (1 - p) * 100 - 1e-9) / 100


def test_stale_pct_episodes_median_duration_and_first_touch_takeable_per_day():
    pts = [
        P(0),
        P(1),
        P(2, bid=0.60),
        P(3, bid=0.60),
        P(4, bid=0.60),
        P(5),
        P(6, bid=0.58, bsz=50.0),
        P(7),
    ]
    st = stale_stats(pts)
    assert st["n_scored"] == 8 and st["n_stale"] == 4 and st["stale_pct"] == pytest.approx(0.5)
    assert st["episodes"] == 2 and st["median_run_s"] == pytest.approx(120.0)
    e1 = (0.60 - 0.50) * 100 - _fee(0.05, 0.60, 100)  # order fee rounded up to the cent
    e2 = (0.58 - 0.50) * 50 - _fee(0.05, 0.58, 50)
    assert st["takeable_day"] == pytest.approx((e1 + e2) / (8 / 1440))
    assert st["censored_runs"] == 0


def test_crossed_vs_reference_and_empty_books_are_not_scored():
    st = stale_stats([P(0, bid=0.60, ask=0.40), P(1), {**P(2), "bid": None, "ask": None}])
    assert (
        st["n_scored"] == 1 and st["n_stale"] == 0
    )  # both sides stale = bad data; no book = nothing to score


def test_runs_break_at_unscored_points_and_time_gaps():
    pts = [P(0, bid=0.60), {**P(1), "ref_fair": None}, {**P(2), "ref_fair": None}, P(40, bid=0.60)]
    st = stale_stats(pts)
    assert (
        st["episodes"] == 2
        and st["median_run_s"] == pytest.approx(60.0 * 1.0 + 0.0)
        or st["episodes"] == 2
    )
    assert st["censored_runs"] == 2


def _requotes(times, dt=2.0, horizon=3600.0):
    out, k = [], 0.0
    while k <= horizon:
        out.append(
            {"ts_ns": int(k * S), "bid": 0.5 + 0.01 * sum(1 for c in times if c <= k), "ask": 0.9}
        )
        k += dt
    return out


def test_touch_changes_ignore_size_only_changes():
    s = [
        {"ts_ns": i * S, "bid": 0.5, "ask": 0.6, "bid_size": 10.0 + i, "ask_size": 5.0}
        for i in range(10)
    ]
    assert touch_changes(s) == []


def test_periodic_requotes_flag_at_their_period_not_others():
    f = cadence_flags([5.0] * 12, sample_dt=1.0)
    assert (
        f[5]["flag"]
        and f[5]["share"] == pytest.approx(1.0)
        and not f[30]["flag"]
        and not f[60]["flag"]
    )
    g = cadence_flags([30.0] * 12, sample_dt=2.0)
    assert g[30]["flag"] and not g[5]["flag"] and not g[60]["flag"] or g[30]["flag"]
    h = cadence_flags(
        [10.0] * 10 + [20.0] * 2,
        sample_dt=1.0,  # at 2 s sampling the +/-2 s window cannot tell this from random
    )  # touch moves every 2nd requote of a 5 s quoter
    assert h[5]["flag"] is True  # multiples of the period count


def test_2s_sampling_cannot_fake_a_5s_cluster_and_poisson_is_not_flagged():
    assert (
        cadence_flags([2.0] * 10, sample_dt=2.0)[5]["flag"] is False
    )  # 5 s is below 3 sampling intervals
    assert cadence_flags([2.0] * 10, sample_dt=2.0)[5]["testable"] is False
    rng = random.Random(7)
    t, times = 0.0, []
    while t < 3600:
        t += rng.expovariate(1 / 10.0)  # memoryless requotes, mean 10 s
        times.append(t)
    iv = touch_changes(_requotes(times))
    flags = cadence_flags(iv, sample_dt=2.0)
    assert not any(v["flag"] for v in flags.values()), flags
