import math

import pytest

from bmm.pricing import (
    Quote, black76_call, digital_call, digital_call_smile, digital_delta,
    digital_from_call_spread, digital_gamma, gemini_maker_rebate, hedge_contracts,
    interp_total_variance, kalshi_taker_fee, pascal_fees, to_forecastex_orders,
    two_sided_quote,
)

F, K, T, SIG = 6500.0, 6520.0, 1 / 252, 0.16


def test_call_spread_converges_to_flat_digital():
    h = 0.5
    cs = digital_from_call_spread(
        black76_call(F, K - h, T, SIG), black76_call(F, K + h, T, SIG), K - h, K + h)
    assert cs == pytest.approx(digital_call(F, K, T, SIG), abs=1e-4)


def test_smile_digital_matches_call_spread_on_skewed_smile():
    skew = -0.8  # dsig/d(ln K): equity-like
    vol = lambda k: SIG + skew * math.log(k / F)  # noqa: E731
    h = 0.5
    cs = digital_from_call_spread(
        black76_call(F, K - h, T, vol(K - h)), black76_call(F, K + h, T, vol(K + h)), K - h, K + h)
    assert digital_call_smile(F, K, T, vol) == pytest.approx(cs, abs=2e-4)
    # negative skew makes the upside digital worth MORE than flat-vol
    assert digital_call_smile(F, K, T, vol) > digital_call(F, K, T, vol(K))


def test_delta_and_gamma_match_finite_difference():
    e = 0.05
    up, dn, mid = (digital_call(F + e, K, T, SIG), digital_call(F - e, K, T, SIG),
                   digital_call(F, K, T, SIG))
    assert digital_delta(F, K, T, SIG) == pytest.approx((up - dn) / (2 * e), rel=1e-4)
    assert digital_gamma(F, K, T, SIG) == pytest.approx((up - 2 * mid + dn) / e**2, rel=1e-3)


def test_yes_plus_no_is_discount_factor():
    df = 0.9995
    yes = digital_call(F, K, T, SIG, df)
    no = df - yes
    assert yes + no == pytest.approx(df)


def test_hedge_sizing_back_of_envelope():
    # ATM daily SPX digital: ~0.006 $/pt per contract; 1,000 contracts ~ 1.2 MES
    d = digital_delta(F, F, T, SIG)
    assert 0.003 < d < 0.01
    assert hedge_contracts(1000, d, 5.0) == pytest.approx(1000 * d / 5.0)


def test_total_variance_interp_endpoints_and_arb_check():
    assert interp_total_variance(0.1, 0.1, 0.2, 0.2, 0.25) == pytest.approx(0.2)
    assert interp_total_variance(0.2, 0.1, 0.2, 0.2, 0.25) == pytest.approx(0.25)
    with pytest.raises(ValueError):
        interp_total_variance(0.15, 0.1, 0.30, 0.2, 0.10)


def test_two_sided_quote_ticks_and_skew():
    q = two_sided_quote(0.503, 0.012)
    assert q == Quote(0.49, 0.52)
    long_q = two_sided_quote(0.503, 0.012, inventory=100, skew_per_contract=0.0001)
    assert long_q.yes_bid < q.yes_bid or long_q.yes_ask < q.yes_ask
    assert two_sided_quote(0.995, 0.01).yes_ask is None  # clipped above 0.99


def test_forecastex_buy_only_translation():
    orders = to_forecastex_orders(Quote(0.47, 0.53))
    assert orders == [
        {"action": "BUY", "right": "C", "limit": 0.47},
        {"action": "BUY", "right": "P", "limit": 0.47},
    ]


def test_fee_schedules():
    assert kalshi_taker_fee(0.50, 100) == pytest.approx(1.75)
    assert kalshi_taker_fee(0.50, 1) == pytest.approx(0.02)  # rounds up
    t, r = pascal_fees(0.50, 100)
    assert t == pytest.approx(0.50) and r == pytest.approx(0.125)
    assert gemini_maker_rebate(0.50, 100) == pytest.approx(0.525)
