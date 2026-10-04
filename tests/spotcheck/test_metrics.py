import math
from datetime import date

import polars as pl
import pytest

from bmm.clock import ct_to_ns
from bmm.pricing import black76_call
from bmm.spotcheck import metrics
from bmm.spotcheck.analyze import LadderRow, OptQuote, edges
from bmm.spotcheck.metrics import edge_frame, moneyness_bucket, ttf_bucket
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
    r.update(snapshot_id=sid, ts_ns=ts, product=product, question_date=D, fix_ts_ns=FIX,
             strike=strike, yes_bid=yb, yes_ask=ya, bid_size=bs, ask_size=asz, crossed=crossed,
             payout_scale=1.0)
    r.update(kw)
    return r


def _opt_rows(sid, ts, chain, role="match", mismatch=False, expiry=FIX, sigma=0.16,
              product="p", F=F0):
    rows = []
    for q in chain:
        r = dict.fromkeys(OPTIONS_SCHEMA)
        r.update(snapshot_id=sid, ts_ns=ts, product=product, underlying_con_id=1,
                 expiry_ts_ns=expiry, expiry_role=role, expiry_mismatch=mismatch,
                 strike=q.strike, call_bid=q.call_bid, call_ask=q.call_ask, und_px=F,
                 atm_sigma=sigma)
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
    opt = (_opt_rows("s0", ts, far, role="above", mismatch=True, expiry=FIX + 600 * MIN)
           + _opt_rows("s0", ts, chain, role="below", mismatch=True, expiry=FIX - 5 * MIN))
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
            "product": "p", "snapshot_id": f"s{r.get('i', i)}", "ts_ns": FIX + r.get("i", i) * MIN,
            "fix_ts_ns": FIX + 10**12, "strike": 100.0, "fee": 0.02, "fair": 0.5, "lower": 0.4,
            "upper": 0.6, "yes_bid": 0.5, "yes_ask": 0.52, "mid_gap": r["gap"] / 100,
            "take_buy_edge": None, "take_sell_edge": None, "arb_buy_edge": None,
            "arb_sell_edge": None, "bid_size": 40, "ask_size": 40, "F": 100.0, "sigma": 0.1,
            "T": 0.01, "m_bucket": "1-2", "ttf_bucket": "<30m", "expiry_mismatch": False,
            "crossed": False, "stale_chain": False, "opt_snapshot_id": "o", "opt_ts_ns": 0,
            "opt_expiry_ts_ns": 0,
        }
        d.update({k: v for k, v in r.items() if k not in ("gap", "i")})
        out.append(d)
    return pl.DataFrame(out, schema=metrics.EF_SCHEMA)
