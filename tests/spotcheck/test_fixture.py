import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from bmm.clock import ct_to_ns
from bmm.spotcheck.contracts import PRODUCTS, match_expiry, option_class_ok
from bmm.spotcheck.fixture import FixtureSource, make_session
from bmm.spotcheck.source import SourceError

D = date(2026, 10, 5)
ROOT = Path(__file__).resolve().parents[2]
SESSION = ROOT / "tests/fixtures/spotcheck/session.json"


def run(c):
    return asyncio.run(c)


def test_make_session_is_deterministic():
    a = json.dumps(make_session(D, seed=7).to_dict())
    assert a == json.dumps(make_session(D, seed=7).to_dict())
    assert a != json.dumps(make_session(D, seed=8).to_dict())


def test_json_round_trips(tmp_path):
    src = FixtureSource(make_session(D))
    p = tmp_path / "s.json"
    src.to_json(p)
    again = FixtureSource.from_json(p)
    assert again.session == src.session
    again.to_json(tmp_path / "t.json")
    assert (tmp_path / "t.json").read_text() == p.read_text()


def test_checked_in_fixture_is_current_and_small():
    assert SESSION.stat().st_size < 2_000_000
    assert json.loads(SESSION.read_text()) == make_session(D).to_dict()


def test_ladder_has_calls_and_puts_and_details():
    src = FixtureSource(make_session(D))
    q, details = run(src.ladder(PRODUCTS["forecastex-spx"], D))
    assert {x.right for x in q} == {"C", "P"} and len(q) == 2 * len(
        src.session.products["forecastex-spx"].ladder_strikes
    )
    assert len(details) == len(q) and details[0]["lastTradeTime"] == "15:00:00"
    cme, _ = run(src.ladder(PRODUCTS["cme-gold"], D))
    assert {x.right for x in cme} == {"C", "P"}
    c, p = cme[0].quote, cme[1].quote  # same strike: put book mirrors the call book
    assert p.bid == pytest.approx(1 - c.ask, abs=1e-12) and p.ask == pytest.approx(
        1 - c.bid, abs=1e-12
    )


def test_cme_gold_has_no_expiry_near_the_fix():
    spec = PRODUCTS["cme-gold"]
    fix = ct_to_ns(D, *spec.fix_ct)
    src = FixtureSource(make_session(D), clock=lambda: fix - 600 * 10**9)
    fut = run(src.reference_future(spec, D, [], None))
    ex = run(src.option_expiries(spec, fut))
    decoys = [e for e in ex if e.trading_class == "ECGC"]
    assert len(decoys) == 1 and decoys[0].expiry_ts_ns == fix  # the event contract's own class
    vanilla = [e for e in ex if option_class_ok(spec, e.trading_class)]
    assert vanilla and all(abs(e.expiry_ts_ns - fix) > 60 * 10**9 for e in vanilla)
    assert (
        match_expiry(fix, fut.con_id, ex).chosen[0][0].trading_class == "ECGC"
    )  # unfiltered: wrong
    m = match_expiry(fix, fut.con_id, vanilla)
    assert m and m.mismatch


def test_spx_matches_and_decoy_chain_is_ignored():
    spec = PRODUCTS["forecastex-spx"]
    fix = ct_to_ns(D, *spec.fix_ct)
    src = FixtureSource(make_session(D), clock=lambda: fix - 600 * 10**9)
    fut = run(src.reference_future(spec, D, [], None))
    ex = run(src.option_expiries(spec, fut))
    assert len(ex) == 2
    m = match_expiry(fix, fut.con_id, ex)
    assert m and not m.mismatch and m.chosen[0][0].underlying_con_id == fut.con_id
    strikes = run(src.listed_strikes(spec, m.chosen[0][0]))
    assert len(run(src.calls(spec, m.chosen[0][0], strikes[:11]))) == 11


def test_clock_selects_snapshot_and_expiry_disappears_after_fix():
    spec = PRODUCTS["forecastex-ndx"]
    fix = ct_to_ns(D, *spec.fix_ct)
    now = [fix - 7 * 60 * 10**9 - 5]  # between two 1-min snapshots
    src = FixtureSource(make_session(D), clock=lambda: now[0])
    f1 = run(src.reference_future(spec, D, [], None))
    now[0] = fix + 60 * 10**9
    assert all(e.expiry_ts_ns <= now[0] for e in run(src.option_expiries(spec, f1)))  # passed
    now[0] = fix - 4 * 3600 * 10**9
    with pytest.raises(SourceError):
        run(src.ladder(spec, D))  # before the first snapshot


def test_every_product_has_crossed_row_trades_and_post_fix_quotes():
    s = make_session(D)
    for key, p in s.products.items():
        fix = ct_to_ns(D, *PRODUCTS[key].fix_ct)
        assert any(
            r[0] is not None and r[1] is not None and r[0] >= r[1]
            for sn in p.snaps
            for r in sn.ladder
        ), key
        post = [sn for sn in p.snaps if fix < sn.ts_ns <= fix + 30 * 60 * 10**9]
        assert len(post) == 30 and all(sn.ladder for sn in post)
        vols = sorted({r[6] for sn in post for r in sn.ladder if r[6] is not None})
        assert vols == [100.0, 105.0, 110.0], key  # two trades after the fix
