import math

import pytest

from bmm.scan.metrics import (
    adverse_jump,
    calibrate_lambda,
    fills_from_shares,
    mean_abs_move,
    resample_trades,
    tail_move_1h,
)
from bmm.scan.models import Trade

M = 60 * 10**9
H = 60 * M


def test_resample_trades_last_price_forward_filled():
    t = [Trade(0, 0.4, 1.0, "buy"), Trade(12 * M, 0.5, 1.0, "sell")]
    assert resample_trades(t) == [(0, 0.4), (5 * M, 0.4), (10 * M, 0.5)]
    assert resample_trades([]) == []


def test_mean_abs_move_30m():
    s = [(i * 5 * M, 0.5 if i < 6 else 0.6) for i in range(12)]
    v, n = mean_abs_move(s, 30 * M)
    assert v == pytest.approx(0.1) and n == 6
    assert mean_abs_move(s[:3], 30 * M) == (None, 0)


def test_tail_move_1h_uses_fine_and_scales_coarse():
    fine = [(i * 5 * M, 0.5 if i < 100 else 0.6) for i in range(288)]
    assert tail_move_1h(fine, []) == pytest.approx(0.1)  # 12 of 276 windows straddle the jump
    flat = [(i * 5 * M, 0.5) for i in range(288)]
    coarse = [(i * 3 * H, 0.5 if i < 10 else 0.6) for i in range(20)]
    v = tail_move_1h(flat, coarse)
    assert v is not None and 0 < v <= 0.1 / math.sqrt(3) + 1e-9  # 3h move scaled to 1h
    assert tail_move_1h(flat[:5], []) is None  # too few points


def test_calibrate_lambda_clamped():
    assert calibrate_lambda([(10, -0.02, 0.05)]) == pytest.approx(0.4)
    assert calibrate_lambda([(10, -0.5, 0.05)]) == 1.0
    assert calibrate_lambda([(10, -0.0001, 0.05)]) == 0.05
    assert calibrate_lambda([]) is None


@pytest.mark.parametrize(
    ("side", "px", "jc", "p99", "want"),
    [
        ("bid", 0.30, "high", 0.01, 0.30),  # high: full distance to 0
        ("ask", 0.80, "high", 0.01, 0.20),  # high: full distance to 1
        ("bid", 0.30, "normal", 0.05, 0.05),
        ("bid", 0.30, "normal", 0.50, 0.30),  # capped at the boundary
        ("ask", 0.80, "normal", None, 0.20),  # unknown tail -> worst case
    ],
)
def test_adverse_jump(side, px, jc, p99, want):
    assert adverse_jump(side, px, jc, p99) == pytest.approx(want)


def test_fills_from_shares():
    assert fills_from_shares(100.0, 10 * H, 0, None) == pytest.approx(10.0)  # lifetime average
    assert fills_from_shares(100.0, 10 * H, 0, (8 * H, 60.0)) == pytest.approx(20.0)  # delta
    assert fills_from_shares(100.0, 10 * H, 0, (10 * H - 30 * M, 99.0)) == pytest.approx(10.0)
    assert fills_from_shares(100.0, 10 * H, 0, (8 * H, 150.0)) == pytest.approx(10.0)  # reset
    assert fills_from_shares(100.0, 10 * H, None, None) is None
