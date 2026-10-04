import math

import pytest

from bmm.spotcheck.normalise import ScaleError, cme_row, forecastex_row, raw_quote


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


def test_cme_scale_1_and_100():
    a = cme_row(4000.0, raw_quote(0.41, 0.44, 10, 12), payout_scale=1.0)
    b = cme_row(4000.0, raw_quote(41.0, 44.0, 10, 12), payout_scale=100.0)
    assert a.row.yes_bid == pytest.approx(b.row.yes_bid, abs=1e-12) == pytest.approx(0.41, abs=1e-12)
    assert a.row.yes_ask == pytest.approx(0.44, abs=1e-12) and a.row.ask_size == 12


def test_cme_scale_mismatch_raises():
    with pytest.raises(ScaleError):
        cme_row(4000.0, raw_quote(41.0, 44.0, 1, 1), payout_scale=1.0)
