import asyncio

import pytest

from bmm.scan.config import VenueCfg
from bmm.scan.futures_depth import analyze_futures, collect, depth_metrics, our_share, rank_rows
from bmm.scan.models import Book, Level
from bmm.scan.schedule import reward_hours
from bmm.scan.venues.common import iso_ns
from bmm.scan.venues.pm_us import PmUs

NOW = iso_ns("2026-10-06T12:00:00Z")


def prow(
    slug,
    program="cfb_futures_20261001",
    period="daily_event",
    pool=36,
    target=7500,
    disc=0.3,
    end=None,
):
    tp = {"programId": program, "programType": "liquidityProgram", "start": "2026-10-02T00:00:00Z",
          "rewardPool": pool, "status": "active", "discountFactor": disc, "targetSize": target, "period": period}  # fmt: skip
    if end:
        tp["end"] = end
    return {"marketSlug": slug, "timePeriods": [tp], "category": "SPR", "subcategory": "FOOTBALL"}


class PagedPM:
    def __init__(self, rows, page=2):
        self.rows, self.page, self.calls = rows, page, []

    async def get_json(self, url, params=None):
        self.calls.append(dict(params or {}))
        start = int((params or {}).get("page_token") or 0)
        nxt = str(start + self.page) if start + self.page < len(self.rows) else ""
        return {"programs": self.rows[start : start + self.page], "nextPageToken": nxt}


def adapter(client, **cfg):
    a = PmUs(client, VenueCfg("pm_us", {"max_markets": 100, **cfg}))
    a._now = lambda: NOW
    return a


def test_pages_with_snake_case_params_to_the_end():
    rows = [prow(f"ev-2027-a-{i}") for i in range(5)]
    c = PagedPM(rows)
    progs = asyncio.run(adapter(c).all_programs())
    assert len(progs) == 5
    assert [x.get("page_token") for x in c.calls] == [None, "2", "4"]  # followed nextPageToken
    assert all(
        "page_size" in x and "pageSize" not in x and "pageToken" not in x for x in c.calls
    )  # camelCase is ignored


def test_event_pool_is_prorated_equally_across_instruments():
    rows = [prow(f"aachc-cfb-2026-11-28-mostpasstds-{t}") for t in "abcd"] + [
        prow("aachc-cfb-2026-11-28-wins-x")
    ]
    by = {p.market_id: p for p in asyncio.run(adapter(PagedPM(rows, 50)).all_programs())}
    p = by["aachc-cfb-2026-11-28-mostpasstds-a"]
    assert (
        p.event_n == 4
        and p.event_key == "aachc-cfb-2026-11-28-mostpasstds"
        and p.pool_event_usd == 36
    )
    assert p.pool_usd == pytest.approx(9.0) and p.R_usd_hr == pytest.approx(36 / 24 / 4)
    assert reward_hours(p, NOW) == pytest.approx(
        24.0
    )  # schedule rate is pro-rated too: still 24 effective hours
    assert (
        by["aachc-cfb-2026-11-28-wins-x"].event_n == 1
        and by["aachc-cfb-2026-11-28-wins-x"].pool_usd == 36
    )


def test_game_periods_are_not_prorated():
    rows = [
        prow(
            f"aec-nfl-x-y-2026-10-0{i}",
            program="nfl_live",
            period="live",
            pool=680,
            end="2026-10-03T00:00:00Z",
        )
        for i in range(1, 4)
    ]
    p = asyncio.run(adapter(PagedPM(rows, 50)).all_programs())[0]
    assert (
        p.event_n == 1 and p.pool_usd == 680
    )  # only the futures (daily_event) pool is per event, per the docs


def test_triage_by_prorated_pool_per_day():
    big = [prow("solo-2027-w-x", pool=36)]
    many = [prow(f"crowd-2027-w-{i}", pool=36) for i in range(20)]
    got = asyncio.run(adapter(PagedPM(big + many, 50), max_markets=1).programs())
    assert [p.market_id for p in got] == [
        "solo-2027-w-x"
    ]  # 1 instrument keeps the whole pool; 20 share it


L = Level


def test_depth_metrics_levels_target_and_one_level_flag():
    m = depth_metrics(
        [L(0.50, 900), L(0.49, 50), L(0.48, 50)], target=1000, discount=0.5, tick=0.01
    )
    assert (m["touch_depth"], m["depth_target"], m["largest_level"]) == (900, 1000, 900)
    assert (
        m["largest_share"] == pytest.approx(0.9)
        and m["one_level"] is True
        and m["largest_offset_ticks"] == 0
    )
    assert m["D"] == pytest.approx(900 + 50 * 0.5 + 50 * 0.25)
    t = depth_metrics([L(0.50, 900), L(0.49, 50), L(0.48, 50)], target=500, discount=0.5, tick=0.01)
    assert t["depth_target"] == 500 and t["largest_level"] == 500  # the walk stops at the target
    flat = depth_metrics(
        [L(0.5 - 0.01 * i, 200) for i in range(5)], target=1000, discount=0.5, tick=0.01
    )
    assert flat["largest_share"] == pytest.approx(0.2) and flat["one_level"] is False
    e = depth_metrics([], target=1000, discount=0.5, tick=0.01)
    assert e["empty"] is True and e["depth_target"] == 0 and e["one_level"] is False


def test_our_share_is_pro_rata_of_scored_depth():
    assert our_share(1000, 937.5) == pytest.approx(1000 / 1937.5)
    assert our_share(0, 10) == 0.0 and our_share(5, 0) == 1.0


class FakeAd:
    def __init__(self, books, bad=()):
        self.books, self.bad = books, set(bad)

    async def book(self, slug):
        if slug in self.bad:
            raise KeyError("shape")
        return self.books[slug]


def test_analyze_futures_rows_per_market_and_side():
    progs = asyncio.run(
        adapter(
            PagedPM([prow("ev-2027-w-a", pool=48), prow("ev-2027-w-b", pool=48)], 50)
        ).all_programs()
    )
    books = {
        "ev-2027-w-a": Book([L(0.40, 5000), L(0.39, 5000)], [L(0.45, 300)]),
        "ev-2027-w-b": Book([], []),
    }
    rows = analyze_futures(progs, books, side_split=0.5, tick=0.01)
    a_bid = next(r for r in rows if r["market_id"] == "ev-2027-w-a" and r["side"] == "bid")
    assert a_bid["R_side_day"] == pytest.approx(48 / 2 * 0.5)  # pool/2 instruments, half per side
    assert (
        a_bid["target"] == 7500 and a_bid["depth_target"] == 7500 and a_bid["touch_depth"] == 5000
    )
    assert a_bid["share_target"] == pytest.approx(7500 / (7500 + a_bid["D"]))
    assert a_bid["share_1000"] == pytest.approx(1000 / (1000 + a_bid["D"]))
    assert a_bid["day_1000"] == pytest.approx(a_bid["R_side_day"] * a_bid["share_1000"])
    assert (
        len(rows) == 4 and next(r for r in rows if r["market_id"] == "ev-2027-w-b")["empty"] is True
    )


def test_rank_by_reward_per_depth_with_a_floor_and_flags():
    rows = [{"key": "a", "R_side_day": 6.0, "depth_target": 100.0, "one_level": False},
            {"key": "b", "R_side_day": 6.0, "depth_target": 3000.0, "one_level": True},
            {"key": "c", "R_side_day": 1.0, "depth_target": 0.0, "one_level": False}]  # fmt: skip
    r = rank_rows(rows)
    assert [x["key"] for x in r] == ["c", "a", "b"]  # c: 1/max(0,1)=1.0, a: 0.06, b: 0.002


def test_collect_skips_a_market_whose_book_fails():
    progs = asyncio.run(
        adapter(PagedPM([prow("ev-2027-w-a"), prow("ev-2027-w-b")], 50)).all_programs()
    )
    ok = Book([L(0.40, 100)], [L(0.45, 100)])
    got = asyncio.run(
        collect(
            FakeAd({"ev-2027-w-a": ok, "ev-2027-w-b": ok}, bad={"ev-2027-w-b"}),
            progs,
            concurrency=2,
        )
    )
    assert set(got) == {"ev-2027-w-a"}
