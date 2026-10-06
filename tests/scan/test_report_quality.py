import polars as pl
import pytest

from bmm.scan import store
from bmm.scan.report import quality_section, quality_stats

from .test_report import DATE, TS

MIN = 60 * 10**9


def snaps(market, bids, ds, ref=0.5, rq="venue_ref", tier="fast", fee=0.05, venue="v"):
    rows = []
    for i, (b, d) in enumerate(zip(bids, ds, strict=True)):
        for side in ("bid", "ask"):
            rows.append({"ts_ns": TS + i * 5 * MIN, "venue": venue, "market_id": market, "side": side,
                         "bid": b, "ask": 0.52, "bid_size": 100.0, "ask_size": 100.0, "ref_fair": ref,
                         "taker_fee_coeff": fee, "tier": tier, "reference_quality": rq, "D_side": d / 2})  # fmt: skip
    return rows


def test_stale_ranking_uses_only_fast_tier_venue_ref_markets(tmp_path):
    rows = (
        snaps("REF1", [0.50, 0.50, 0.60, 0.60, 0.50, 0.50], [10, 12, 9, 11, 10, 10])
        + snaps("REF2", [0.50] * 6, [10] * 6)
        + snaps("NOREF", [0.70] * 6, [10, 30, 10, 30, 10, 30], ref=None, rq="model_needed")
        + snaps("SLOW", [0.70] * 6, [10] * 6, tier="full")
    )
    store.write("snapshots", rows, TS, tmp_path)
    q = quality_stats(DATE, tmp_path)
    assert set(q["stale"]) == {"v:REF1", "v:REF2"}  # reference markets, fast tier only
    assert q["stale"]["v:REF1"]["stale_pct"] == pytest.approx(2 / 6)
    assert q["stale"]["v:REF2"]["stale_pct"] == 0.0
    assert q["n_ref"] == 2
    assert q["churn"]["v:NOREF"] > q["churn"]["v:REF2"] == 0.0  # D churn covers every market
    sec = quality_section(DATE, tmp_path)
    top = sec.split("stale_pct")[1]
    assert top.index("REF1") < top.index("REF2") and "takeable" in sec


def test_section_states_when_no_market_has_a_reference(tmp_path):
    store.write("snapshots", snaps("X", [0.5] * 6, [10] * 6, ref=None, rq="none"), TS, tmp_path)
    sec = quality_section(DATE, tmp_path)
    assert "no market has a venue reference" in sec and "D churn" in sec


def test_cadence_from_burst_samples(tmp_path):
    base = TS
    rows = [{"ts_ns": base + t * 10**9, "venue": "v", "market_id": "B1", "bid": 0.5 + 0.01 * (t // 5),
             "ask": 0.6, "bid_size": 10.0, "ask_size": 10.0} for t in range(61)]  # fmt: skip
    store.write("bursts", rows, TS, tmp_path)
    store.write("snapshots", snaps("B1", [0.5] * 6, [10] * 6, ref=None, rq="none"), TS, tmp_path)
    q = quality_stats(DATE, tmp_path)
    assert q["cadence"]["v:B1"]["flags"][5]["flag"] is True
    assert "clustering at 5s" in quality_section(DATE, tmp_path)
    assert isinstance(pl.DataFrame(), pl.DataFrame)  # polars import used by seeds elsewhere
