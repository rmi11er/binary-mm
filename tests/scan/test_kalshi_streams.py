import asyncio
from datetime import UTC, datetime

import pytest

from bmm.scan.config import VenueCfg
from bmm.scan.schedule import pool_remaining, reward_hours
from bmm.scan.venues.common import classify, iso_ns, ticker_category
from bmm.scan.venues.kalshi import Kalshi

MIN = 60 * 10**9
NOW = iso_ns("2026-10-06T01:20:00Z")


def iso(ns):
    return datetime.fromtimestamp(ns / 1e9, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def row(i, ticker, start_ns, dur_min, pool=20.0, target=300):
    return {"id": f"id{i}-{ticker}", "market_ticker": ticker, "start_date": iso(start_ns),
            "end_date": iso(start_ns + dur_min * MIN), "period_reward": int(pool * 1e4),
            "target_size_fp": f"{target}.00", "discount_factor_bps": 5000, "paid_out": False,
            "incentive_type": "liquidity", "market_id": "x"}  # fmt: skip


class Paged:
    def __init__(self, active, upcoming=(), page=200, stuck=False):
        self.rows = {"active": list(active), "upcoming": list(upcoming)}
        self.page, self.stuck, self.calls = page, stuck, []

    async def get_json(self, url, params=None):
        self.calls.append(dict(params or {}))
        rows = self.rows[params["status"]]
        start = 0 if self.stuck else int(params.get("cursor") or 0)
        page = rows[start : start + self.page]
        nxt = (
            "x" if self.stuck else (str(start + self.page) if start + self.page < len(rows) else "")
        )
        return {"incentive_programs": page, "next_cursor": nxt}


def adapter(client, **cfg):
    a = Kalshi(client, VenueCfg("kalshi", {"max_markets": 100, **cfg}))
    a._now = lambda: NOW
    return a


def stream_rows(series="KXPALLADIUM15M", n_up=100, per_window=1, pool=20.0):
    start = NOW - 5 * MIN
    active = [row(0, f"{series}-A0", start, 15, pool)]
    up = [row(j * 10 + k, f"{series}-A{j}-{k}", start + 15 * MIN * j, 15, pool)
          for j in range(1, n_up + 1) for k in range(per_window)]  # fmt: skip
    return active, up


def test_pages_every_program_by_cursor_and_stops_on_a_stuck_cursor():
    active = [row(i, f"KXS{i}-1", NOW - 60 * MIN, 24 * 60) for i in range(5)]
    c = Paged(active, page=2)
    progs = asyncio.run(adapter(c).programs())
    assert len(progs) == 5
    cursors = [x.get("cursor") for x in c.calls if x["status"] == "active"]
    assert cursors == [None, "2", "4"]  # followed next_cursor to the end
    stuck = Paged(active, stuck=True)
    asyncio.run(adapter(stuck).programs())
    assert len([x for x in stuck.calls if x["status"] == "active"]) <= 2  # no new rows -> stop


def test_recurring_short_windows_become_a_stream_with_summed_pools():
    active, up = stream_rows()
    p = asyncio.run(adapter(Paged(active, up)).programs())[0]
    assert p.stream_windows == 97 and p.R_usd_hr == pytest.approx(80.0)
    assert reward_hours(p, NOW) == pytest.approx(
        24.0
    )  # contiguous windows cover the whole next 24h
    assert pool_remaining(p, NOW) == pytest.approx(80.0 * 24)


def test_windows_with_several_strikes_count_once():
    active, up = stream_rows(per_window=3)
    p = asyncio.run(adapter(Paged(active, up)).programs())[0]
    assert pool_remaining(p, NOW) == pytest.approx(80.0 * 24)  # not x3


def test_fewer_than_three_windows_is_not_a_stream():
    active, up = stream_rows(n_up=1)
    p = asyncio.run(adapter(Paged(active, up)).programs())[0]
    assert p.stream_windows == 0 and reward_hours(p, NOW) == pytest.approx(10 / 60)


def test_upcoming_rows_duplicating_active_ids_are_ignored():
    active, _ = stream_rows(n_up=0)
    progs = asyncio.run(adapter(Paged(active, active)).programs())
    assert len(progs) == 1


def test_triage_domain_series_first_then_pool_per_day():
    h = 60 * MIN
    active = [
        row(1, "KXZZZ-1", NOW - h, 24 * 60, pool=500.0, target=1000),
        row(2, "KXAGICO-1", NOW - h, 168 * 60, pool=100.0, target=1000),
        row(3, "KXGOLD15M-X", NOW - 5 * MIN, 15),
        row(4, "KXUSDJPY15M-X", NOW - 5 * MIN, 15),
        row(5, "KXCPI-1", NOW - h, 24 * 60, pool=10.0, target=1000),
    ]
    two = {x.market_id for x in asyncio.run(adapter(Paged(active), max_markets=2).programs())}
    assert two == {"KXGOLD15M-X", "KXUSDJPY15M-X"}  # metals/FX before any larger non-domain pool
    four = {x.market_id for x in asyncio.run(adapter(Paged(active), max_markets=4).programs())}
    assert four == {
        "KXGOLD15M-X",
        "KXUSDJPY15M-X",
        "KXCPI-1",
        "KXZZZ-1",
    }  # then the biggest pool/day


@pytest.mark.parametrize(
    ("ticker", "cat"),
    [
        ("KXPALLADIUM15M-26OCT052115-15", "metals"),
        ("KXGOLDH-26OCT0610-T3000", "metals"),
        ("KXCOPPERD-26OCT06-T5", "metals"),
        ("KXGOLD-26OCT06-T3000", "metals"),
        ("KXUSDJPY15M-26OCT052115-30", "fx"),
        ("KXWTI-26OCT06-T70", "energy"),
        ("KXFED-26DEC-T4.00", "rates"),
        ("KXINXU-26OCT06-T5000", "equity_index"),
        ("KXGOLDMAX-26OCT-1", "metals"),
        ("KXFEDCHAIRNOM-26-1", None),
        ("KXDOWNTOWN-26-1", None),
        ("KXRATECUTCOUNT-26DEC-T3", "rates"),
        ("KXAGICO-26OCT-1", None),
        ("KXTRUMPSAY-26OCT-1", None),
        ("KXGOLDCARD-26-1", None),
        ("KXCONCACAFGC-26-1", None),
        ("KXCOPPEROPEN-26-1", None),
        ("KXGOLDENBACHELOR-26-1", None),
    ],
)
def test_ticker_category_has_no_metals_false_positives(ticker, cat):
    assert ticker_category(ticker) == cat


@pytest.mark.parametrize(
    ("title", "is_metals"),
    [
        ("Gold Hourly", True),
        ("Price of gold", True),
        ("Daily Copper", True),
        ("Copper Weekly Price", True),
        ("Palladium 15-minute", True),
        ("How high will Gold get by end of year?", True),
        ("CONCACAF Gold Cup", False),
        ("Gold card", False),
        ("Gold cards sold", False),
        ("Copper Mountain Opening", False),
        ("Tariffs on copper in effect before date", False),
        ("When will insulated copper cable face Section 232 restrictions?", False),
        ("BOE gold withdrawals", False),
        ("BTC vs Gold", False),
        ("who will win the gold bachelor", False),
        ("Will gold hit a record high in 2026?", True),
        ("Will silver hit $50 by December?", True),
        ("Gold price above $4,000 after tariffs?", True),
        ("Will Copper win the race? Up next", False),
        ("What will Trump say this week?", False),
        ("Will an AGI company IPO?", False),
    ],
)
def test_metals_title_rule(title, is_metals):
    assert (classify(title) == "metals") is is_metals


def test_overlapping_programs_in_a_series_are_not_a_stream():
    h = 60 * MIN
    a_ = row(1, "KXSERH-A", NOW - h, 24 * 60, pool=240.0)  # a day-long program ...
    b_ = row(2, "KXSERH-B", NOW + 9 * h, 60, pool=10.0)  # ... overlapping hourly ones
    up = [
        row(3, "KXSERH-C", NOW + 10 * h, 60, pool=10.0),
        row(4, "KXSERH-D", NOW + 11 * h, 60, pool=10.0),
    ]
    progs = {p.market_id: p for p in asyncio.run(adapter(Paged([a_, b_], up)).programs())}
    assert progs["KXSERH-A"].stream_windows == 0 and progs["KXSERH-B"].stream_windows == 0
    assert reward_hours(progs["KXSERH-A"], NOW) == pytest.approx(
        23.0
    )  # its own window only, never > 24
    assert reward_hours(progs["KXSERH-B"], NOW) == pytest.approx(1.0)


def test_series_without_a_recurring_suffix_is_not_a_stream():
    h = 60 * MIN
    act = [row(1, "KXMLBGAME-A", NOW - 10 * MIN, 60)]
    up = [
        row(2 + j, f"KXMLBGAME-{j}", NOW + 50 * MIN + j * h, 60) for j in range(5)
    ]  # three different games
    p = asyncio.run(adapter(Paged(act, up)).programs())[0]
    assert p.stream_windows == 0 and reward_hours(p, NOW) == pytest.approx(50 / 60)


def test_alerts_for_hourly_and_daily_metals_even_when_triage_drops_them():
    act = [
        row(1, "KXGOLDH-26OCT0610-T3000", NOW - 5 * MIN, 60),
        row(2, "KXGOLDCARD-1", NOW - 5 * MIN, 24 * 60, pool=900.0),
    ]
    up = [row(3, "KXCOPPERD-26OCT07-T5", NOW + 30 * MIN, 24 * 60)]
    a = adapter(Paged(act, up), max_markets=1)
    asyncio.run(a.programs())
    got = {(x["series"], x["status"]) for x in a.alerts}
    assert got == {
        ("KXGOLDH", "active"),
        ("KXCOPPERD", "upcoming"),
    }  # KXGOLDCARD is not on the watch list
    quiet = adapter(Paged([row(9, "KXGOLDCARD-1", NOW - 5 * MIN, 60)]))
    asyncio.run(quiet.programs())
    assert quiet.alerts == []


def test_per_series_cap_keeps_one_series_from_eating_the_triage_budget():
    gas = [row(i, f"KXAAAGASW-26OCT-T{i}", NOW - 60 * MIN, 24 * 60) for i in range(10)]
    gold = [row(20 + i, f"KXGOLDD-26OCT-T{i}", NOW - 60 * MIN, 24 * 60) for i in range(5)]
    got = asyncio.run(adapter(Paged(gas + gold), max_markets=6, series_cap=3).programs())
    per = {}
    for p in got:
        per[p.market_id.split("-")[0]] = per.get(p.market_id.split("-")[0], 0) + 1
    assert per == {"KXAAAGASW": 3, "KXGOLDD": 3}
