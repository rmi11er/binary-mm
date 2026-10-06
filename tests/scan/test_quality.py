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


def test_stale_pct_episodes_median_duration_and_takeable_per_day():
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
    assert st["episodes"] == 2
    assert st["median_run_s"] == pytest.approx(
        120.0
    )  # runs of 3 min and 1 min (last-first + one spacing)
    edge1 = 0.60 - 0.50 - 0.05 * 0.60 * 0.40  # 100 displayed
    edge2 = 0.58 - 0.50 - 0.05 * 0.58 * 0.42  # 50 displayed
    # one fill per episode at its first stale snapshot, over the observed span (8 min)
    assert st["takeable_day"] == pytest.approx((100 * edge1 + 50 * edge2) / (8 / 1440))


def test_unscorable_snapshots_are_not_counted():
    pts = [P(0), {**P(1, bid=0.9), "ref_fair": None}, {**P(2, bid=0.9), "fee_coeff": None}, P(3)]
    st = stale_stats(pts)
    assert st["n_scored"] == 2 and st["n_stale"] == 0
    assert stale_stats([{**P(0), "ref_fair": None}]) is None


def test_both_sides_stale_adds_both_edges():
    st = stale_stats([P(0, bid=0.60, ask=0.40), P(1)])
    bid_edge = (0.60 - 0.50 - 0.05 * 0.6 * 0.4) * 100
    ask_edge = (0.50 - 0.40 - 0.05 * 0.4 * 0.6) * 100
    assert st["takeable_day"] == pytest.approx((bid_edge + ask_edge) / (2 / 1440))


def test_touch_changes_and_cadence_clustering_at_5s():
    samples = [{"ts_ns": t * S, "bid": 0.5 + 0.01 * (t // 5), "ask": 0.6, "bid_size": 10.0, "ask_size": 10.0}
               for t in range(31)]  # bid requotes every 5 s, sampled every 1 s  # fmt: skip
    iv = touch_changes(samples)
    assert iv == [5.0] * (len(iv)) and len(iv) == 5
    f = cadence_flags(iv, sample_dt=1.0, min_n=3)
    assert f[5]["flag"] is True and f[30]["flag"] is False and f[60]["flag"] is False
    assert f[5]["share"] == pytest.approx(1.0)


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
