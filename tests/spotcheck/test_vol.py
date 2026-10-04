import math

import pytest

from bmm.pricing import black76_call
from bmm.spotcheck.analyze import OptQuote
from bmm.spotcheck.vol import atm_sigma, implied_vol, strike_window, year_frac

F, T = 6500.0, 1 / 252
vol = lambda k: 0.16 - 0.8 * math.log(k / F)


@pytest.mark.parametrize("k", [0.97 * F, F, 1.03 * F])
def test_implied_vol_roundtrip(k):
    assert implied_vol(F, k, T, black76_call(F, k, T, 0.18)) == pytest.approx(0.18, abs=1e-6)


def test_implied_vol_outside_no_arb_range():
    assert implied_vol(F, 6400.0, T, 99.0 - 1e-9) is None  # below intrinsic (100)
    assert implied_vol(F, 6400.0, T, F) is None
    assert implied_vol(F, 6400.0, T, F + 1.0) is None


def test_atm_sigma_on_skewed_chain():
    chain = []
    for i in range(-30, 31):
        k = F + i * 5.0
        c = black76_call(F, k, T, vol(k))
        chain.append(OptQuote(k, c, c))
    s = atm_sigma(chain, F, T)
    assert s is not None and s == pytest.approx(vol(F), abs=2e-3)


def test_atm_sigma_empty_chain():
    assert atm_sigma([], F, T) is None


def test_strike_window():
    lo, hi = strike_window(6500.0, 0.16, 1 / 252)
    w = 4 * 0.16 * math.sqrt(1 / 252)
    assert lo == pytest.approx(6500 * math.exp(-w), abs=1e-6)
    assert hi == pytest.approx(6500 * math.exp(w), abs=1e-6)


def test_year_frac():
    assert year_frac(0, 0) == 1e-9
    assert year_frac(0, 365 * 86400 * 10**9) == pytest.approx(1.0, abs=1e-12)
    assert year_frac(10, 0) == 1e-9
