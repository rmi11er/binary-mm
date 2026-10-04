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
    assert year_frac(10, 0) == 1e-9  # within 1 s: clamp
    with pytest.raises(ValueError):
        year_frac(5 * 10**9, 0)  # expired by 5 s


def test_local_black76_matches_pricing_module():
    from bmm.spotcheck.vol import black76_call as local

    for F, K, T, s in [(6500, 6500, 1 / 252, 0.16), (6500, 6560, 1 / 365, 0.2), (4000, 3900, 0.01, 0.3)]:
        assert local(F, K, T, s) == pytest.approx(black76_call(F, K, T, s), abs=1e-9)


def test_atm_sigma_skips_bad_nearest_quote():
    good = [OptQuote(k, c - 0.1, c + 0.1) for k in (6480.0, 6520.0, 6540.0)
            for c in [black76_call(F, k, T, 0.16)]]
    bad = OptQuote(6500.0, 0.0, 5.0)  # zero bid at the nearest strike
    crossed = OptQuote(6501.0, 9.0, 8.0)
    assert atm_sigma([bad, crossed, *good], F, T) == pytest.approx(0.16, abs=2e-3)
    assert atm_sigma([bad, crossed], F, T) is None


def test_strike_window_covers_ladder_and_extends_one_listed_strike():
    listed = [float(k) for k in range(6300, 6720, 10)]
    lo, hi = strike_window(F, 0.16, 1 / 525600, ladder_strikes=[6480.0, 6525.0], listed=listed)
    assert (lo, hi) == (6470.0, 6530.0)  # ladder range, then one listed strike beyond each edge
    lo2, hi2 = strike_window(F, 0.16, T)
    assert lo2 < F < hi2 and strike_window(F, 0.16, T, listed=listed)[0] <= lo2
