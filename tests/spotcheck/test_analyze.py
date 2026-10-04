import math

import pytest

from bmm.pricing import black76_call, digital_call_smile
from bmm.spotcheck.analyze import (
    LadderRow,
    OptQuote,
    capturable_dollars,
    edges,
    fair_band,
)

F, T = 6500.0, 1 / 252
vol = lambda k: 0.16 - 0.8 * math.log(k / F)


def chain(step=5.0, half_spread=0.0):
    out = []
    for i in range(-30, 31):
        k = F + i * step
        c = black76_call(F, k, T, vol(k))
        out.append(OptQuote(k, max(c - half_spread, 0.0), c + half_spread))
    return out


def test_fair_matches_smile_digital_off_grid():
    for K in (6487.0, 6500.0, 6512.5, 6540.0):
        fb = fair_band(chain(), K)
        assert fb is not None
        assert fb.fair == pytest.approx(digital_call_smile(F, K, T, vol), abs=3e-3)
        assert fb.lower <= fb.fair <= fb.upper


def test_bounds_widen_with_option_spreads_and_contain_truth():
    K = 6512.5
    true = digital_call_smile(F, K, T, vol)
    tight, wide = fair_band(chain(), K), fair_band(chain(half_spread=0.25), K)
    assert tight.lower <= true <= tight.upper
    assert wide.lower <= true <= wide.upper
    assert (wide.upper - wide.lower) > (tight.upper - tight.lower)


def test_unbracketed_strike_returns_none():
    assert fair_band(chain(), F + 10_000) is None


def test_edges_flag_rich_wing_after_fees():
    K = 6560.0
    fair = fair_band(chain(), K).fair
    rich = LadderRow(K, yes_bid=round(fair + 0.05, 2), yes_ask=round(fair + 0.07, 2),
                     bid_size=50, ask_size=50)
    fairish = LadderRow(6500.0, yes_bid=0.50, yes_ask=0.53, bid_size=50, ask_size=50)
    rows = edges([rich, fairish], chain(half_spread=0.05), fee_per_side=0.01)
    by_k = {r.strike: r for r in rows}
    assert by_k[K].take_sell_edge > 0.03
    assert by_k[K].take_buy_edge < 0
    assert by_k[6500.0].take_sell_edge < 0.01
    assert capturable_dollars(rows, threshold=0.02) == pytest.approx(
        by_k[K].take_sell_edge * 50)
