import pytest

from bmm.scan.models import Program
from bmm.scan.schedule import make_schedule, pool_remaining, reward_hours

H = 3600 * 10**9
NOW = 1_800_000_000 * 10**9


def P(rate, segs):
    return Program(
        "v",
        "m",
        rate,
        None,
        None,
        1.0,
        None,
        "x",
        False,
        0.0,
        0.0,
        "h",
        schedule=make_schedule(segs),
    )


def test_program_ending_in_one_hour_pays_one_hour_not_24():
    p = P(100.0, [(NOW - H, NOW + H, 100.0)])
    assert reward_hours(p, NOW) == pytest.approx(1.0)
    assert pool_remaining(p, NOW) == pytest.approx(100.0)  # rate x hours left, not rate x 24


def test_long_program_is_capped_at_the_24h_horizon():
    assert reward_hours(P(5.0, [(NOW - H, NOW + 100 * H, 5.0)]), NOW) == pytest.approx(24.0)


def test_program_starting_later_counts_only_its_overlap():
    assert reward_hours(P(5.0, [(NOW + 6 * H, NOW + 30 * H, 5.0)]), NOW) == pytest.approx(18.0)


def test_future_program_outside_horizon_pays_nothing():
    assert reward_hours(P(5.0, [(NOW + 30 * H, NOW + 40 * H, 5.0)]), NOW) == 0.0


def test_multi_period_schedule_weights_by_rate_relative_to_current():
    p = P(10.0, [(NOW - H, NOW + 2 * H, 10.0), (NOW + 2 * H, NOW + 26 * H, 20.0)])
    assert reward_hours(p, NOW) == pytest.approx(
        2 * 1 + 22 * 2
    )  # effective hours at the current rate
    assert pool_remaining(p, NOW) == pytest.approx(10 * 2 + 20 * 24)


def test_open_ended_period_assumed_to_continue_one_pool_per_day():
    p = P(1.5, [(NOW - 5 * H, None, 1.5)])
    assert reward_hours(p, NOW) == pytest.approx(24.0)
    assert pool_remaining(p, NOW) == pytest.approx(36.0)


def test_started_unknown_start_uses_now():
    assert reward_hours(P(2.0, [(None, NOW + 3 * H, 2.0)]), NOW) == pytest.approx(3.0)


def test_no_schedule_is_legacy_24h_and_zero_rate_is_safe():
    p = Program("v", "m", 3.0, None, None, 1.0, None, "x", False, 0.0, 0.0, "h")
    assert reward_hours(p, NOW) == 24.0 and pool_remaining(p, NOW) == pytest.approx(72.0)
    assert reward_hours(P(0.0, [(NOW, NOW + H, 0.0)]), NOW) == 0.0
