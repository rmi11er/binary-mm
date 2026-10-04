import math

import pytest

from bmm.spotcheck.normalise import (
    ScaleError,
    check_ladder_scale,
    clean,
    cme_row,
    forecastex_row,
    raw_quote,
)


def test_forecastex_yes_ask_is_one_minus_no_bid():
    r = forecastex_row(6500.0, raw_quote(0.47, None, 30, 0), raw_quote(0.50, None, 25, 0))
    assert r.row.yes_bid == pytest.approx(0.47, abs=1e-12)
    assert r.row.yes_ask == pytest.approx(0.50, abs=1e-12)
    assert (r.row.bid_size, r.row.ask_size) == (30, 25)
    assert r.raw_no is not None and r.raw_no.bid == 0.50 and not r.crossed


def test_missing_quotes_become_none():
    r = forecastex_row(6500.0, raw_quote(math.nan, -1.0, 0, 0), raw_quote(-1.0, math.nan, 0, 0))
    assert r.row.yes_bid is None and r.row.yes_ask is None
    assert r.row.bid_size == 0 and r.row.ask_size == 0


def test_crossed_row_flagged():
    r = forecastex_row(6500.0, raw_quote(0.52, None, 5, 0), raw_quote(0.50, None, 5, 0))
    assert r.crossed  # yes_bid 0.52 >= yes_ask 0.50


def test_locked_row_flagged():
    r = forecastex_row(6500.0, raw_quote(0.50, None, 5, 0), raw_quote(0.50, None, 5, 0))
    assert r.crossed  # YES bid + NO bid == 1


def test_negative_sizes_floored():
    q = raw_quote(0.4, 0.5, -1, -7)
    assert (q.bid_size, q.ask_size) == (0, 0)


def test_clean_rejects_inf():
    assert clean(math.inf) is None and clean(-math.inf) is None


def test_zero_size_side_is_absent_but_raw_kept():
    r = forecastex_row(1.0, raw_quote(0.47, None, 0, 0), raw_quote(0.50, None, 0, 0))
    assert r.row.yes_bid is None and r.row.yes_ask is None
    assert r.raw_yes.bid == 0.47 and r.raw_no is not None and r.raw_no.bid == 0.50
    c = cme_row(1.0, raw_quote(0.41, 0.44, 0, 5), None, 1.0)
    assert c.row.yes_bid is None and c.row.yes_ask == pytest.approx(0.44, abs=1e-12)


def test_locked_books_flagged_for_every_cent_pair():
    for yb in range(1, 100):
        r = forecastex_row(1.0, raw_quote(yb / 100, None, 5, 0), raw_quote((100 - yb) / 100, None, 5, 0))
        assert r.crossed, yb  # YES bid + NO bid == 1 exactly, despite float error
        ok = forecastex_row(1.0, raw_quote(yb / 100, None, 5, 0), raw_quote((99 - yb) / 100 or 0.01, None, 5, 0))
        assert not ok.crossed or yb == 99


def test_forecastex_price_above_one_raises():
    with pytest.raises(ScaleError):
        forecastex_row(1.0, raw_quote(0.47, None, 5, 0), raw_quote(47.0, None, 5, 0))


def test_cme_scale_1_and_100():
    a = cme_row(4000.0, raw_quote(0.41, 0.44, 10, 12), None, payout_scale=1.0)
    b = cme_row(4000.0, raw_quote(41.0, 44.0, 10, 12), None, payout_scale=100.0)
    assert a.row.yes_bid == pytest.approx(b.row.yes_bid, abs=1e-12) == pytest.approx(0.41, abs=1e-12)
    assert a.row.yes_ask == pytest.approx(0.44, abs=1e-12) and a.row.ask_size == 12


def test_cme_scale_mismatch_raises():
    with pytest.raises(ScaleError):
        cme_row(4000.0, raw_quote(41.0, 44.0, 1, 1), None, payout_scale=1.0)


def test_cme_uses_put_book():
    # call 0.40/0.50; put 0.45/0.52 -> implied YES bid 1-0.52=0.48 beats 0.40, YES ask 1-0.45=0.55 loses
    r = cme_row(4000.0, raw_quote(0.40, 0.50, 7, 8), raw_quote(0.45, 0.52, 3, 4), 1.0)
    assert r.row.yes_bid == pytest.approx(0.48, abs=1e-12) and r.row.bid_size == 4
    assert r.row.yes_ask == pytest.approx(0.50, abs=1e-12) and r.row.ask_size == 8
    assert r.raw_no is not None and r.raw_no.ask == 0.52 and not r.crossed
    tight = cme_row(4000.0, raw_quote(0.40, 0.50, 7, 8), raw_quote(0.55, 0.60, 3, 4), 1.0)
    assert tight.row.yes_ask == pytest.approx(0.45, abs=1e-12) and tight.row.ask_size == 3
    crossed = cme_row(4000.0, raw_quote(0.60, 0.70, 7, 8), raw_quote(0.45, 0.50, 3, 4), 1.0)
    assert crossed.crossed  # yes_bid 0.60 vs yes_ask 0.55


def test_ladder_level_scale_check():
    rows = [cme_row(float(k), raw_quote(0.4, 0.45, 1, 1), None, 100.0) for k in range(3)]
    with pytest.raises(ScaleError):
        check_ladder_scale(rows, 100.0)  # scale 100 on a $1-priced ladder
    check_ladder_scale(rows, 1.0)
    ok = [cme_row(4000.0, raw_quote(41.0, 44.0, 1, 1), None, 100.0)]
    check_ladder_scale(ok, 100.0)
