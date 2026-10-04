import math
from datetime import date

import polars as pl
import pytest

from bmm.clock import ct_to_ns
from bmm.pricing import black76_call
from bmm.spotcheck import metrics
from bmm.spotcheck.analyze import LadderRow, OptQuote, edges
from bmm.spotcheck.metrics import (
    arb_table,
    edge_frame,
    gap_stats,
    ladder_quality,
    mismatch_share,
    moneyness_bucket,
    persistence,
    post_fix_activity,
    size_at_gap_strikes,
    taker_stats,
    ttf_bucket,
)
from bmm.spotcheck.schema import FUTURES_SCHEMA, LADDER_SCHEMA, OPTIONS_SCHEMA

D = date(2026, 10, 5)
FIX = ct_to_ns(D, 15, 0)
MIN = 60 * 1_000_000_000
F0, T0 = 6500.0, 1 / 252


def _vol(k):
    return 0.16 - 0.8 * math.log(k / F0)


def _chain(step=5.0):
    out = []
    for i in range(-30, 31):
        k = F0 + i * step
        c = black76_call(F0, k, T0, _vol(k))
        out.append(OptQuote(k, max(c - 0.05, 0.0), c + 0.05))
    return out


def _ladder_row(sid, ts, strike, yb, ya, bs=40, asz=40, crossed=False, product="p", **kw):
    r = dict.fromkeys(LADDER_SCHEMA)
    r.update(
        snapshot_id=sid,
        ts_ns=ts,
        product=product,
        question_date=D,
        fix_ts_ns=FIX,
        strike=strike,
        yes_bid=yb,
        yes_ask=ya,
        bid_size=bs,
        ask_size=asz,
        crossed=crossed,
        payout_scale=1.0,
    )
    r.update(kw)
    return r


def _opt_rows(
    sid, ts, chain, role="match", mismatch=False, expiry=FIX, sigma=0.16, product="p", F=F0
):
    rows = []
    for q in chain:
        r = dict.fromkeys(OPTIONS_SCHEMA)
        r.update(
            snapshot_id=sid,
            ts_ns=ts,
            product=product,
            underlying_con_id=1,
            expiry_ts_ns=expiry,
            expiry_role=role,
            expiry_mismatch=mismatch,
            strike=q.strike,
            call_bid=q.call_bid,
            call_ask=q.call_ask,
            und_px=F,
            atm_sigma=sigma,
        )
        rows.append(r)
    return rows


def _tables(ladder, options, futures=()):
    return {
        "ladder": pl.DataFrame(ladder, schema=LADDER_SCHEMA),
        "options": pl.DataFrame(options, schema=OPTIONS_SCHEMA),
        "futures": pl.DataFrame(list(futures), schema=FUTURES_SCHEMA),
    }


# ---------------------------------------------------------------- 11a


def test_moneyness_buckets():
    assert moneyness_bucket(6500, 6500, 0.16, 1 / 252) == "<0.5"
    sd = 0.16 * math.sqrt(1 / 252)
    assert moneyness_bucket(6500 * math.exp(1.5 * sd), 6500, 0.16, 1 / 252) == "1-2"
    assert moneyness_bucket(6500 * math.exp(0.7 * sd), 6500, 0.16, 1 / 252) == "0.5-1"
    assert moneyness_bucket(6500 * math.exp(-2.5 * sd), 6500, 0.16, 1 / 252) == ">2"
    assert moneyness_bucket(6500, 6500, None, 1 / 252) == "n/a"


def test_ttf_buckets():
    assert ttf_bucket(FIX + 1, FIX) == "post-fix"
    assert ttf_bucket(FIX, FIX) == "post-fix"
    assert ttf_bucket(FIX - 5 * 3600 * 10**9 // 1, FIX) == ">4h"
    assert ttf_bucket(FIX - 3 * 3600 * 10**9, FIX) == "1-4h"
    assert ttf_bucket(FIX - 45 * MIN, FIX) == "30-60m"
    assert ttf_bucket(FIX - 5 * MIN, FIX) == "<30m"


def test_edge_frame_matches_analyze_edges():
    chain = _chain()
    K = 6560.0
    lad, opt = [], []
    for i, (ts, yb) in enumerate([(FIX - 20 * MIN, 0.12), (FIX - 19 * MIN, 0.13)]):
        sid = f"s{i}"
        lad.append(_ladder_row(sid, ts, K, yb, yb + 0.02))
        opt += _opt_rows(sid, ts, chain)
    ef = edge_frame(_tables(lad, opt), 0.01)
    assert ef.height == 2
    for i, row in enumerate(ef.sort("ts_ns").iter_rows(named=True)):
        want = edges([LadderRow(K, lad[i]["yes_bid"], lad[i]["yes_ask"], 40, 40)], chain, 0.01)[0]
        assert row["mid_gap"] == pytest.approx(want.mid_gap, abs=1e-12)
        assert row["take_buy_edge"] == pytest.approx(want.take_buy_edge, abs=1e-12)
        assert row["expiry_mismatch"] is False
        assert row["ttf_bucket"] == "<30m"
        assert row["fee"] == 0.01


def test_edge_frame_prefers_match_and_flags_mismatch():
    chain = _chain()
    ts = FIX - 10 * MIN
    lad = [_ladder_row("s0", ts, 6560.0, 0.12, 0.14)]
    opt = _opt_rows("s0", ts, chain, role="above", mismatch=True, expiry=FIX + 24 * 60 * MIN)
    ef = edge_frame(_tables(lad, opt), 0.01)
    assert ef["expiry_mismatch"].to_list() == [True]
    opt2 = opt + _opt_rows("s0", ts, chain, role="match")
    assert edge_frame(_tables(lad, opt2), 0.01)["expiry_mismatch"].to_list() == [False]


def test_edge_frame_uses_nearest_chain_when_no_match():
    chain = _chain()
    ts = FIX - 10 * MIN
    lad = [_ladder_row("s0", ts, 6560.0, 0.12, 0.14)]
    far = [OptQuote(q.strike, q.call_bid + 5, q.call_ask + 5) for q in chain]
    opt = _opt_rows("s0", ts, far, role="above", mismatch=True, expiry=FIX + 600 * MIN) + _opt_rows(
        "s0", ts, chain, role="below", mismatch=True, expiry=FIX - 5 * MIN
    )
    ef = edge_frame(_tables(lad, opt), 0.01)
    want = edges([LadderRow(6560.0, 0.12, 0.14, 40, 40)], chain, 0.01)[0]
    assert ef["fair"][0] == pytest.approx(want.fair, abs=1e-12)
    assert ef["opt_expiry_ts_ns"][0] == FIX - 5 * MIN


def test_edge_frame_post_fix_uses_last_pre_fix_chain():
    chain = _chain()
    t1, t2 = FIX - MIN, FIX + 2 * MIN
    lad = [_ladder_row("s0", t1, 6560.0, 0.12, 0.14), _ladder_row("s1", t2, 6560.0, 0.01, 0.02)]
    ef = edge_frame(_tables(lad, _opt_rows("s0", t1, chain)), 0.01).sort("ts_ns")
    assert ef.height == 2
    post = ef.row(1, named=True)
    assert post["ttf_bucket"] == "post-fix"
    assert post["stale_chain"] is True
    assert post["opt_snapshot_id"] == "s0"
    assert post["fair"] == pytest.approx(ef["fair"][0], abs=1e-12)
    assert post["m_bucket"] == ef["m_bucket"][0]


def test_edge_frame_no_chain_pre_fix_is_skipped():
    lad = [_ladder_row("s0", FIX - MIN, 6560.0, 0.12, 0.14)]
    assert edge_frame(_tables(lad, []), 0.01).height == 0


def test_load_reads_date_range(tmp_path):
    from bmm.spotcheck.schema import ParquetSink

    sink = ParquetSink(tmp_path)
    sink.append(D, "ladder", [_ladder_row("s0", FIX - MIN, 6560.0, 0.1, 0.2)])
    sink.append(date(2026, 10, 6), "ladder", [_ladder_row("s1", FIX - MIN, 6560.0, 0.1, 0.2)])
    t = metrics.load(tmp_path, D, D)
    assert t["ladder"].height == 1
    assert t["options"].height == 0 and t["futures"].height == 0
    assert metrics.load(tmp_path, D, date(2026, 10, 6))["ladder"].height == 2


def _ef(rows):
    """Literal edge frame: dicts with product, strike, gap (cents), plus optional overrides."""
    out = []
    for i, r in enumerate(rows):
        d = {
            "product": "p",
            "snapshot_id": f"s{r.get('i', i)}",
            "ts_ns": FIX + r.get("i", i) * MIN,
            "fix_ts_ns": FIX + 10**12,
            "strike": 100.0,
            "fee": 0.02,
            "fair": 0.5,
            "lower": 0.4,
            "upper": 0.6,
            "yes_bid": 0.5,
            "yes_ask": 0.52,
            "mid_gap": r["gap"] / 100,
            "take_buy_edge": None,
            "take_sell_edge": None,
            "arb_buy_edge": None,
            "arb_sell_edge": None,
            "bid_size": 40,
            "ask_size": 40,
            "F": 100.0,
            "sigma": 0.1,
            "T": 0.01,
            "m_bucket": "1-2",
            "ttf_bucket": "<30m",
            "expiry_mismatch": False,
            "crossed": False,
            "stale_chain": False,
            "opt_snapshot_id": "o",
            "opt_ts_ns": 0,
            "opt_expiry_ts_ns": 0,
        }
        d.update({k: v for k, v in r.items() if k not in ("gap", "i")})
        out.append(d)
    return pl.DataFrame(out, schema=metrics.EF_SCHEMA)


def test_crossed_rows_excluded_from_gap_stats():
    ef = _ef([{"gap": 4}, {"gap": 4}, {"gap": 50, "crossed": True}])
    assert ef.height == 3
    gs = gap_stats(ef)
    assert gs["n"].sum() == 2
    assert gs["median_abs_gap"][0] == pytest.approx(0.04, abs=1e-12)


# ---------------------------------------------------------------- 11b


def test_persistence_runs():
    ef = _ef([{"gap": g, "i": i} for i, g in enumerate([4, 4, 4, 1, 4, 4])])
    p = persistence(ef)
    assert p.height == 1
    assert p["n_runs"][0] == 2
    assert p["median_run"][0] == pytest.approx(2.5, abs=1e-12)
    assert p["max_run"][0] == 3


def test_persistence_missing_snapshot_breaks_run():
    ef = _ef(
        [{"gap": 4, "i": 0}, {"gap": 4, "i": 1}, {"gap": 4, "i": 3}]
        + [{"gap": 0, "i": 2, "strike": 200.0}]
    )
    assert persistence(ef)["max_run"][0] == 2


def test_pct_gt_3c():
    ef = _ef([{"gap": 4}, {"gap": -5}, {"gap": 3.5}, {"gap": 1}])
    gs = gap_stats(ef)
    assert gs["pct_gt_3c"][0] == pytest.approx(0.75, abs=1e-12)
    assert gs["pct_gt_2c"][0] == pytest.approx(0.75, abs=1e-12)
    assert gs["n"][0] == 4
    # iqr of |gap| = [1,3.5,4,5]c -> q75 - q25 (linear) = 4.25 - 2.875
    assert gs["iqr"][0] == pytest.approx(0.01375, abs=1e-9)


def test_gap_stats_by_ttf_includes_post_fix_only_when_asked():
    ef = _ef([{"gap": 4}, {"gap": 4, "ttf_bucket": "post-fix"}])
    assert gap_stats(ef)["n"].sum() == 1
    g2 = gap_stats(ef, by=("m_bucket", "ttf_bucket"))
    assert g2["n"].sum() == 2


def test_taker_best_edge_is_max_over_strikes_and_both_sides():
    rows = [
        {"gap": 0, "i": 0, "strike": 1.0, "take_buy_edge": 0.005, "take_sell_edge": -0.02},
        {
            "gap": 0,
            "i": 0,
            "strike": 2.0,
            "take_buy_edge": -0.01,
            "take_sell_edge": 0.015,
            "bid_size": 10,
        },
        {"gap": 0, "i": 1, "strike": 1.0, "take_buy_edge": -0.01, "take_sell_edge": -0.02},
    ]
    t = taker_stats(_ef(rows), fees=(0.02, 0.03))
    r02 = t.filter(pl.col("fee") == 0.02).row(0, named=True)
    # snapshot 0 best = 0.015 (>1c, not >2c); snapshot 1 best = -0.01 (<=0)
    assert r02["pct_snapshots_best_edge_gt_1c"] == pytest.approx(0.5, abs=1e-12)
    assert r02["pct_snapshots_best_edge_gt_2c"] == pytest.approx(0.0, abs=1e-12)
    assert r02["pct_snapshots_best_edge_le_0"] == pytest.approx(0.5, abs=1e-12)
    # capturable snapshot 0 = 0.005*40 + 0.015*10; snapshot 1 = 0 -> median = mean of both
    assert r02["capturable_median"] == pytest.approx(0.5 * (0.2 + 0.15), abs=1e-12)
    # fee 0.03 shifts every edge down 1c: best 0.005 in snapshot 0
    r03 = t.filter(pl.col("fee") == 0.03).row(0, named=True)
    assert r03["pct_snapshots_best_edge_gt_1c"] == pytest.approx(0.0, abs=1e-12)
    assert r03["pct_snapshots_best_edge_le_0"] == pytest.approx(0.5, abs=1e-12)


def test_size_at_gap_strikes_uses_taker_side():
    ef = _ef(
        [
            {"gap": 4, "bid_size": 50, "ask_size": 5},
            {"gap": -4, "bid_size": 50, "ask_size": 7},
            {"gap": 1, "bid_size": 999},
        ]
    )
    s = size_at_gap_strikes(ef)
    assert s["median_size"][0] == pytest.approx(28.5, abs=1e-12)  # median(50 bid-side, 7 ask-side)


def _arb_ef(ts_lag_ns=0, width=0.1):
    sid = "s0"
    ts = FIX - MIN
    opt = [
        {
            **dict.fromkeys(OPTIONS_SCHEMA),
            "snapshot_id": sid,
            "ts_ns": ts - ts_lag_ns,
            "product": "p",
            "expiry_ts_ns": 7,
            "expiry_role": "match",
            "strike": k,
            "call_bid": 10.0 - width / 2,
            "call_ask": 10.0 + width / 2,
        }
        for k in (90.0, 95.0, 100.0, 105.0, 110.0)
    ]
    ef = _ef(
        [
            {
                "gap": 4,
                "strike": 100.0,
                "arb_buy_edge": 0.01,
                "ts_ns": ts,
                "opt_snapshot_id": sid,
                "opt_ts_ns": ts - ts_lag_ns,
                "opt_expiry_ts_ns": 7,
            }
        ]
    )
    tables = {"options": pl.DataFrame(opt, schema=OPTIONS_SCHEMA)}
    return ef, tables


def test_arb_table_not_suspect_when_fresh_and_tight():
    ef, t = _arb_ef()
    a = arb_table(ef, t)
    assert a.height == 1 and a["suspect"][0] is False
    assert a["opt_rel_width"][0] == pytest.approx(0.01, abs=1e-9)


def test_arb_table_flags_wide_options():
    ef, t = _arb_ef(width=6.0)  # 6 / 10 = 0.6 > 0.5
    assert arb_table(ef, t)["suspect"][0] is True


def test_arb_table_flags_stale_options():
    ef, t = _arb_ef(ts_lag_ns=6 * 10**9)
    a = arb_table(ef, t)
    assert a["suspect"][0] is True
    assert a["opt_ts_lag_ms"][0] == pytest.approx(6000.0, abs=1e-6)


def test_arb_table_skips_non_positive_and_crossed():
    ef, t = _arb_ef()
    assert arb_table(ef.with_columns(arb_buy_edge=pl.lit(-0.01)), t).height == 0
    assert arb_table(ef.with_columns(crossed=pl.lit(True)), t).height == 0


def _lad_tables(rows):
    return {"ladder": pl.DataFrame(rows, schema=LADDER_SCHEMA)}


def test_post_fix_activity_counts_volume_steps_after_fix():
    rows = []
    for i, (off, vol, last) in enumerate(
        [(-2, 100.0, None), (-1, 100.0, None), (1, 105.0, 0.6), (2, 105.0, 0.6), (3, 110.0, 0.62)]
    ):
        rows.append(_ladder_row(f"s{i}", FIX + off * MIN, 100.0, 0.98, 0.99, volume=vol, last=last))
    rows.append(_ladder_row("s9", FIX - MIN, 200.0, None, None))
    t = _lad_tables(rows)
    pa = post_fix_activity(t)
    assert pa["trades_after_fix"][0] == 2
    assert pa["volume_after_fix"][0] == pytest.approx(10.0, abs=1e-12)
    assert pa["quotes_resting"][0] == 3
    q = ladder_quality(t)
    assert q["trades_seen"][0] == 2
    assert q["volume_delta"][0] == pytest.approx(10.0, abs=1e-12)
    assert q["strikes_quoted"][0] == 1


def test_post_fix_trade_price_vs_last_pre_fix_fair():
    rows = [
        _ladder_row("s0", FIX - MIN, 100.0, 0.5, 0.6, volume=100.0),
        _ladder_row("s1", FIX + MIN, 100.0, 0.98, 0.99, volume=105.0, last=0.9),
    ]
    ef = _ef([{"gap": 1, "ts_ns": FIX - MIN, "fix_ts_ns": FIX, "fair": 0.55}])
    pa = post_fix_activity(_lad_tables(rows), ef)
    assert pa["median_trade_px_vs_fair"][0] == pytest.approx(0.35, abs=1e-12)


def test_ladder_quality_counts_crossed():
    rows = [
        _ladder_row("s0", FIX - MIN, 1.0, 0.4, 0.46),
        _ladder_row("s1", FIX - MIN, 2.0, 0.5, 0.4, crossed=True),
    ]
    q = ladder_quality(_lad_tables(rows))
    assert q["crossed_count"][0] == 1
    assert q["median_width"][0] == pytest.approx(0.06, abs=1e-12)
    assert q["median_depth"][0] == pytest.approx(80.0, abs=1e-12)


def test_mismatch_share_and_bias():
    ef = _ef(
        [
            {"gap": 2, "i": 0},
            {"gap": 2, "i": 1},
            {"gap": 6, "i": 2, "expiry_mismatch": True},
            {"gap": 4, "i": 3, "expiry_mismatch": True},
        ]
    )
    m = mismatch_share(ef).row(0, named=True)
    assert m["share_mismatched"] == pytest.approx(0.5, abs=1e-12)
    assert m["median_abs_gap_matched"] == pytest.approx(0.02, abs=1e-12)
    assert m["median_abs_gap_mismatched"] == pytest.approx(0.05, abs=1e-12)
    assert m["bias_abs"] == pytest.approx(0.03, abs=1e-12)
