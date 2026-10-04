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
            "df_flag": False,
            "opt_snapshot_id": "o",
            "opt_ts_ns": 0,
            "opt_expiry_ts_ns": 0,
            "payout_scale": 1.0,
            "expiry_role": "match",
        }
        d.update({k: v for k, v in r.items() if k not in ("gap", "i")})
        d["ladder_ts_ns"] = r.get("ladder_ts_ns", d["ts_ns"])
        # simultaneity / widening defaults: simultaneous, dF = w = 0, widened band == band
        d.setdefault("sim_bucket", "simultaneous" if d["df_flag"] is False else "non-simultaneous")
        if "sim_bucket" not in r:
            d["sim_bucket"] = "simultaneous" if d["df_flag"] is False else "non-simultaneous"
        d.setdefault("dF", 0.0)
        d.setdefault("w", 0.0)
        d.setdefault("lower_w", d["lower"])
        d.setdefault("upper_w", d["upper"])
        d.setdefault("arb_buy_edge_w", d["arb_buy_edge"])
        d.setdefault("arb_sell_edge_w", d["arb_sell_edge"])
        if "gap_outside" not in d:
            g, w = d["mid_gap"], d["w"]
            d["gap_outside"] = None if w is None else (1 if g >= 0 else -1) * max(abs(g) - w, 0.0)
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
    assert a["arb_usd"][0] == pytest.approx(0.01 * 40, abs=1e-12)
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
    assert arb_table(ef.with_columns(arb_buy_edge_w=pl.lit(-0.01)), t).height == 0
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


def test_non_simultaneous_snapshots_leave_the_simultaneous_stats_but_are_kept():
    ef = _ef([{"gap": 4, "i": 0}, {"gap": 4, "i": 1, "df_flag": True}, {"gap": 4, "i": 2}])
    assert gap_stats(ef)["n"].sum() == 2
    assert gap_stats(ef, sim="non-simultaneous")["n"].sum() == 1
    assert persistence(ef)["max_run"][0] == 1  # the non-simultaneous snapshot breaks the run


def test_edge_frame_carries_df_flag_from_futures():
    chain = _chain()
    ts = FIX - 10 * MIN
    lad = [
        _ladder_row("s0", ts, 6560.0, 0.12, 0.14),
        _ladder_row("s1", ts + MIN, 6560.0, 0.12, 0.14),
    ]
    opt = _opt_rows("s0", ts, chain) + _opt_rows("s1", ts + MIN, chain)
    fut = []
    for sid, flag in (("s0", True), ("s1", False)):
        r = dict.fromkeys(FUTURES_SCHEMA)
        r.update(snapshot_id=sid, df_flag=flag)
        fut.append(r)
    ef = edge_frame(_tables(lad, opt, fut), 0.01).sort("ts_ns")
    assert ef["df_flag"].to_list() == [True, False]


def _ef_days(spec):
    """spec: list of (fix_ts_ns, i, gap_cents, extra overrides)."""
    rows = [{"gap": g, "i": i, "fix_ts_ns": fix, **kw} for fix, i, g, kw in spec]
    return _ef(rows)


def test_persistence_does_not_join_runs_across_days():
    day2 = FIX + 86400 * 10**9
    ef = _ef_days([(FIX + 10**12, 0, 4, {}), (FIX + 10**12, 1, 4, {}),
                   (day2 + 10**12, 2, 4, {}), (day2 + 10**12, 3, 4, {})])
    # distinct sessions: fix_ts_ns differs, so two runs of 2 (not one of 4)
    p = persistence(ef)
    assert p["n_runs"][0] == 2 and p["max_run"][0] == 2


def test_persistence_dropped_snapshot_breaks_run_via_grid():
    fix = FIX + 10**12
    ef = _ef_days([(fix, 0, 4, {}), (fix, 1, 4, {}), (fix, 3, 4, {}), (fix, 4, 4, {})])
    grid = pl.DataFrame({"product": ["p"] * 5, "fix_ts_ns": [fix] * 5,
                         "ts_ns": [FIX + i * MIN for i in range(5)]})  # fmt: skip
    assert persistence(ef)["max_run"][0] == 4  # without the grid the gap is invisible
    g = persistence(ef, grid=grid)
    assert g["n_runs"][0] == 2 and g["max_run"][0] == 2  # snapshot 2 was dropped


def test_persistence_mismatched_snapshot_mid_run_breaks_matched_run():
    fix = FIX + 10**12
    ef = _ef_days([(fix, 0, 4, {}), (fix, 1, 4, {}), (fix, 2, 4, {"expiry_mismatch": True}),
                   (fix, 3, 4, {}), (fix, 4, 4, {})])  # fmt: skip
    grid = pl.DataFrame({"product": ["p"] * 5, "fix_ts_ns": [fix] * 5,
                         "ts_ns": [FIX + i * MIN for i in range(5)]})  # fmt: skip
    matched = ef.filter(~pl.col("expiry_mismatch"))
    p = persistence(matched, grid=grid)
    assert p["n_runs"][0] == 2 and p["max_run"][0] == 2
    assert persistence(ef, grid=grid)["max_run"][0] == 5


def test_arb_lag_uses_real_fetch_timestamps_not_slot_ts():
    chain = _chain()
    ts = FIX - 10 * MIN
    K = 6560.0
    lad = [_ladder_row("s0", ts, K, 0.5, 0.52, ladder_ts_ns=ts + 1 * 10**9)]
    opt = _opt_rows("s0", ts, chain)
    for r in opt:
        r["opt_ts_ns"] = ts + 9 * 10**9  # options fetched 8 s after the ladder
    t = _tables(lad, opt)
    ef = edge_frame(t, 0.01)
    assert ef["ladder_ts_ns"][0] == ts + 10**9 and ef["opt_ts_ns"][0] == ts + 9 * 10**9
    a = arb_table(ef, t)
    assert a.height == 1  # venue 0.5 is hugely above the upper bound: sell arb
    assert a["opt_ts_lag_ms"][0] == pytest.approx(8000.0, abs=1e-6)
    assert a["suspect"][0] is True
    lad2 = [_ladder_row("s0", ts, K, 0.5, 0.52, ladder_ts_ns=ts + 9 * 10**9 + 10**9)]
    t2 = _tables(lad2, opt)
    assert arb_table(edge_frame(t2, 0.01), t2)["suspect"][0] is False  # 1 s apart


def test_arb_table_labels_post_fix_stale_and_null_w_as_suspect():
    ef, t = _arb_ef()
    assert arb_table(ef.with_columns(stale_chain=pl.lit(True)), t)["suspect"][0] is True
    assert arb_table(ef.with_columns(w=pl.lit(None, dtype=pl.Float64)), t)["suspect"][0] is True
    a = arb_table(ef.with_columns(ttf_bucket=pl.lit("post-fix")), t)
    assert a["suspect"][0] is True and a["post_fix"][0] is True
    # a flagged snapshot whose move is known (w not null) is handled by widening, not suspect
    f = arb_table(ef.with_columns(df_flag=pl.lit(True), sim_bucket=pl.lit("non-simultaneous")), t)
    assert f["suspect"][0] is False and f["sim_bucket"][0] == "non-simultaneous"


def test_arb_usd_totals_split_suspect_and_scale_by_payout():
    ef, t = _arb_ef()
    ef = ef.with_columns(payout_scale=pl.lit(100.0), arb_sell_edge_w=pl.lit(0.02), bid_size=pl.lit(10))
    a = arb_table(ef, t)
    # (0.01 * ask 40 + 0.02 * bid 10) * 100
    assert a["arb_usd"][0] == pytest.approx((0.4 + 0.2) * 100, abs=1e-9)
    tot = metrics.arb_totals(a)
    assert tot["arb_usd"].sum() == pytest.approx(60.0, abs=1e-6) and tot["suspect"].to_list() == [False]


def test_taker_capturable_scales_with_payout_and_has_threshold_columns():
    rows = [{"gap": 0, "i": 0, "take_buy_edge": 0.03, "take_sell_edge": None, "ask_size": 10,
             "payout_scale": 100.0}]
    r = taker_stats(_ef(rows), fees=(0.02,)).row(0, named=True)
    assert r["capturable_median"] == pytest.approx(0.03 * 10 * 100, abs=1e-9)
    assert r["capturable_gt_2c_median"] == pytest.approx(30.0, abs=1e-9)
    assert r["capturable_gt_1c_median"] == pytest.approx(30.0, abs=1e-9)
    r2 = taker_stats(_ef([{**rows[0], "take_buy_edge": 0.015}]), fees=(0.02,)).row(0, named=True)
    assert r2["capturable_gt_1c_median"] > 0 and r2["capturable_gt_2c_median"] == 0.0


def test_null_df_flag_is_treated_as_flagged():
    chain = _chain()
    ts = FIX - 10 * MIN
    lad = [_ladder_row("s0", ts, 6560.0, 0.12, 0.14), _ladder_row("s1", ts + MIN, 6560.0, 0.12, 0.14),
           _ladder_row("s2", ts + 2 * MIN, 6560.0, 0.12, 0.14)]
    opt = [r for i, t in enumerate((ts, ts + MIN, ts + 2 * MIN)) for r in _opt_rows(f"s{i}", t, chain)]
    fut = []
    for sid, flag in (("s0", None), ("s1", False), ("s2", True)):
        r = dict.fromkeys(FUTURES_SCHEMA)
        r.update(snapshot_id=sid, df_flag=flag)
        fut.append(r)
    ef = edge_frame(_tables(lad, opt, fut), 0.01).sort("ts_ns")
    assert ef["df_flag"].to_list() == [None, False, True]  # raw values kept
    assert ef["sim_bucket"].to_list() == ["non-simultaneous", "simultaneous", "non-simultaneous"]
    assert gap_stats(ef)["n"].sum() == 1  # the simultaneous bucket only


def test_post_fix_always_uses_last_pre_fix_chain():
    chain = _chain()
    far = [OptQuote(q.strike, q.call_bid + 3, q.call_ask + 3) for q in chain]
    t1, t2 = FIX - MIN, FIX + 2 * MIN
    lad = [_ladder_row("s0", t1, 6560.0, 0.12, 0.14), _ladder_row("s1", t2, 6560.0, 0.12, 0.14)]
    opt = _opt_rows("s0", t1, chain) + _opt_rows("s1", t2, far, role="above", mismatch=True,
                                                  expiry=FIX + 600 * MIN)
    ef = edge_frame(_tables(lad, opt), 0.01).sort("ts_ns")
    pre, post = ef.row(0, named=True), ef.row(1, named=True)
    assert post["fair"] == pytest.approx(pre["fair"], abs=1e-12)
    assert post["stale_chain"] is True and post["opt_snapshot_id"] == "s0"
    assert post["expiry_mismatch"] is False


def test_gap_stats_has_per_snapshot_columns():
    rows = [{"gap": 4, "i": 0, "strike": 1.0}, {"gap": 1, "i": 0, "strike": 2.0},
            {"gap": 5, "i": 1, "strike": 1.0}, {"gap": 5, "i": 1, "strike": 2.0}]
    g = gap_stats(_ef(rows)).row(0, named=True)
    assert g["pct_gt_3c"] == pytest.approx(0.75, abs=1e-12)  # per observation
    # snapshot medians: (4,1) -> 2.5c (no), (5,5) -> 5c (yes)
    assert g["pct_snapshots_gt_3c"] == pytest.approx(0.5, abs=1e-12)
    assert g["pct_snapshots_gt_2c"] == pytest.approx(1.0, abs=1e-12)
    assert g["n_snapshots"] == 2


def test_mismatch_tilt_by_role():
    rows = [
        {"gap": 4, "i": 0, "F": 100.0, "strike": 90.0, "expiry_role": "above"},   # K<F: +4
        {"gap": 4, "i": 1, "F": 100.0, "strike": 110.0, "expiry_role": "above"},  # K>F: -4
        {"gap": 6, "i": 2, "F": 100.0, "strike": 90.0, "expiry_role": "below"},
        {"gap": 1, "i": 3, "F": 100.0, "strike": 100.0, "expiry_role": "below"},  # K==F: dropped
    ]
    t = metrics.mismatch_tilt(_ef(rows))
    by = {r["expiry_role"]: r for r in t.iter_rows(named=True)}
    assert by["above"]["median_tilt"] == pytest.approx(0.0, abs=1e-12) and by["above"]["n"] == 2
    assert by["below"]["median_tilt"] == pytest.approx(0.06, abs=1e-12) and by["below"]["n"] == 1


# --------------------------------------------------------------- pre-merge part 2
def _fut_row(sid, before, after, flag):
    r = dict.fromkeys(FUTURES_SCHEMA)
    r.update(snapshot_id=sid, und_px_before=before, und_px_after=after, df_flag=flag)
    return r


def _moved_tables(strikes, ya_over=None, move=2.0, flag=True, sigma=0.16, ts=None, fut=True):
    chain = _chain()
    ts = FIX - 10 * MIN if ts is None else ts
    lad = [_ladder_row("s0", ts, k, 0.5, 0.52) for k in strikes]
    opt = _opt_rows("s0", ts, chain, sigma=sigma)
    futs = [_fut_row("s0", F0, F0 + move, flag)] if fut else []
    return _tables(lad, opt, futs)


def test_w_uses_each_rows_own_strike_delta():
    from bmm.spotcheck.vol import digital_delta, year_frac

    ks = [6495.0, 6500.0, 6505.0]
    ef = edge_frame(_moved_tables(ks), 0.01).sort("strike")
    T = year_frac(FIX - 10 * MIN, FIX)
    Fm = F0 + 1.0
    ws = ef["w"].to_list()
    for k, w in zip(ks, ws, strict=True):
        assert w == pytest.approx(abs(digital_delta(Fm, k, 0.16, T)) * 2.0, abs=1e-12)
    assert len({round(w, 9) for w in ws}) == 3  # different strikes, different widening
    assert ef["dF"].to_list() == [2.0] * 3
    assert ef["sim_bucket"].to_list() == ["non-simultaneous"] * 3


def test_widened_band_contains_the_original_and_is_clipped():
    ef = edge_frame(_moved_tables([6500.0, 6560.0, 6900.0]), 0.01)
    for r in ef.iter_rows(named=True):
        assert r["lower_w"] <= r["lower"] + 1e-15 and r["upper_w"] >= r["upper"] - 1e-15
        assert 0.0 <= r["lower_w"] <= r["upper_w"] <= 1.0
        assert r["lower_w"] == pytest.approx(max(r["lower"] - r["w"], 0.0), abs=1e-12)
        assert r["upper_w"] == pytest.approx(min(r["upper"] + r["w"], 1.0), abs=1e-12)


def test_arb_edge_disappears_under_widening():
    K = 6502.0
    chain = _chain()
    ts = FIX - 10 * MIN
    fb = edges([LadderRow(K, 0.5, 0.52, 40, 40)], chain, 0.01)[0]
    yb = round(fb.upper + 0.01 + 0.005, 4)  # sells above the upper bound by 0.5c after fee
    lad = [_ladder_row("s0", ts, K, yb, yb + 0.02)]
    opt = _opt_rows("s0", ts, chain)
    still = _tables(lad, opt, [_fut_row("s0", F0, F0, False)])
    moved = _tables(lad, opt, [_fut_row("s0", F0, F0 + 2.0, True)])
    e0, e1 = edge_frame(still, 0.01), edge_frame(moved, 0.01)
    assert e0["arb_sell_edge_w"][0] == pytest.approx(yb - fb.upper - 0.01, abs=1e-12)
    assert e0["arb_sell_edge_w"][0] > 0
    assert e1["w"][0] > 0.005 and e1["arb_sell_edge_w"][0] < 0  # the move explains the edge
    assert arb_table(e0, still).height == 1
    assert arb_table(e1, moved).height == 0  # no longer counted
    # unwidened edge is still kept in the frame
    assert e1["arb_sell_edge"][0] == pytest.approx(e0["arb_sell_edge"][0], abs=1e-12)


def test_gap_outside_is_the_gap_beyond_the_widening():
    ef = edge_frame(_moved_tables([6560.0]), 0.01)
    r = ef.row(0, named=True)
    g, w = r["mid_gap"], r["w"]
    want = (1 if g > 0 else -1) * max(abs(g) - w, 0.0)
    assert r["gap_outside"] == pytest.approx(want, abs=1e-12)
    assert abs(r["gap_outside"]) <= abs(g) + 1e-15


def test_w_null_when_move_or_sigma_unknown():
    unknown_after = _tables([_ladder_row("s0", FIX - 10 * MIN, 6560.0, 0.5, 0.52)],
                            _opt_rows("s0", FIX - 10 * MIN, _chain()),
                            [_fut_row("s0", F0, None, None)])  # fmt: skip
    r = edge_frame(unknown_after, 0.01).row(0, named=True)
    assert r["w"] is None and r["dF"] is None and r["gap_outside"] is None
    assert r["sim_bucket"] == "non-simultaneous"
    assert r["lower_w"] == r["lower"] and r["upper_w"] == r["upper"]
    assert arb_table(edge_frame(unknown_after, 0.01), unknown_after).height in (0, 1)
    nosig = _moved_tables([6560.0], sigma=None)
    assert edge_frame(nosig, 0.01)["w"][0] is None  # no sigma, no memo yet


def test_sigma_memo_used_when_snapshot_sigma_missing():
    chain = _chain()
    t0, t1 = FIX - 12 * MIN, FIX - 10 * MIN
    lad = [_ladder_row("s0", t0, 6560.0, 0.5, 0.52), _ladder_row("s1", t1, 6560.0, 0.5, 0.52)]
    opt = _opt_rows("s0", t0, chain, sigma=0.16) + _opt_rows("s1", t1, chain, sigma=None)
    futs = [_fut_row("s0", F0, F0, False), _fut_row("s1", F0, F0 + 1.0, True)]
    ef = edge_frame(_tables(lad, opt, futs), 0.01).sort("ts_ns")
    assert ef["w"][1] is not None and ef["w"][1] > 0


def test_missing_futures_row_means_simultaneous_with_zero_move():
    ef = edge_frame(_moved_tables([6560.0], fut=False), 0.01)
    assert ef["sim_bucket"][0] == "simultaneous" and ef["dF"][0] == 0.0 and ef["w"][0] == 0.0


def test_gap_stats_buckets_and_adjusted():
    ef = _ef([
        {"gap": 4, "i": 0},
        {"gap": 6, "i": 1, "df_flag": True, "w": 0.05, "gap_outside": 0.01},
        {"gap": 6, "i": 2, "df_flag": True, "w": None, "gap_outside": None},
    ])  # fmt: skip
    assert gap_stats(ef)["n"][0] == 1
    ns = gap_stats(ef, sim="non-simultaneous")
    assert ns["n"][0] == 2 and ns["median_abs_gap"][0] == pytest.approx(0.06, abs=1e-12)
    out = gap_stats(ef, sim="non-simultaneous", value="gap_outside")
    assert out["n"][0] == 1 and out["median_abs_gap"][0] == pytest.approx(0.01, abs=1e-12)
    adj = gap_stats(ef, sim="adjusted")  # 4c (sim) + 1c (outside); the null-w row is dropped
    assert adj["n"][0] == 2 and adj["median_abs_gap"][0] == pytest.approx(0.025, abs=1e-12)
    ex = metrics.nonsim_extra(ef).row(0, named=True)
    assert ex["n_w_null"] == 1 and ex["median_w"] == pytest.approx(0.05, abs=1e-12)
    c = metrics.sim_counts(ef)
    assert dict(zip(c["sim_bucket"], c["snapshots"], strict=True)) == {
        "non-simultaneous": 2, "simultaneous": 1}  # fmt: skip
