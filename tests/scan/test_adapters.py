import asyncio
import math
from itertools import pairwise

import pytest

from bmm.scan.config import load
from bmm.scan.models import Book, Level, VenueNotPublic
from bmm.scan.venues import ADAPTERS

CFG = load()


def make(venue, fake_client, **kw):
    cfg = CFG.venue[venue]
    if not cfg.enabled:
        pytest.skip(cfg.skip_reason)
    return ADAPTERS[venue](fake_client(venue, **kw), cfg)


@pytest.mark.parametrize("venue", ["pm_us", "kalshi", "gemini"])
def test_adapter_contract(venue, fake_client):
    a = make(venue, fake_client)
    progs = asyncio.run(a.programs())
    p = progs[0]
    assert progs and all(x.R_usd_hr >= 0 and x.venue == venue for x in progs)
    b = asyncio.run(a.book(p.market_id))
    assert all(0 <= lv.price <= 1 for lv in b.bids + b.asks)
    assert [lv.price for lv in b.bids] == sorted((lv.price for lv in b.bids), reverse=True)
    assert [lv.price for lv in b.asks] == sorted(lv.price for lv in b.asks)
    for side in ("bid", "ask"):
        r = [a.expected_reward(p, s, side, b) for s in range(0, 200, 10)]
        assert r[0] == 0 and all(x <= y + 1e-12 for x, y in pairwise(r))
        assert r[-1] <= p.R_usd_hr + 1e-9
        assert a.qualifying_min(p, side, b) >= p.venue_min
        d, raw = a.scored_depth(p, side, b)
        assert d >= 0 and raw >= 0
    empty = Book([], [])
    assert a.qualifying_min(p, "bid", empty) == max(p.venue_min, math.ceil(p.target_size or 0))
    m = asyncio.run(a.meta(p.market_id))
    assert m.market_id == p.market_id and m.category and m.reference_quality in {
        "venue_ref", "model_needed", "none",
    }  # fmt: skip


@pytest.mark.parametrize("venue", ["pm_us", "kalshi", "gemini"])
def test_401_is_venue_not_public(venue, fake_client):
    a = make(venue, fake_client, status=401)
    with pytest.raises(VenueNotPublic):
        asyncio.run(a.programs())


def test_pm_us_program_and_closed_form(fake_client):
    a = make("pm_us", fake_client)
    p = asyncio.run(a.programs())[0]
    assert (p.target_size, p.discount, p.two_sided) == (7500, 0.3, False)
    assert p.R_usd_hr == pytest.approx(36 / 24)  # daily_event, end null -> 24h (unverified)
    b = asyncio.run(a.book(p.market_id))
    assert b.bids[0].price == 0.01 and b.asks[0].price == 0.02
    d, raw = a.scored_depth(p, "bid", b)
    assert d == pytest.approx(7500.0) and raw == pytest.approx(135973.36)  # target-capped; touch
    c = 0.00005
    cf = a.s_star_closed_form(p, "bid", b, c)
    best = max(range(5000), key=lambda s: a.expected_reward(p, s, "bid", b) - c * s)
    assert cf == pytest.approx(best, abs=1.0)
    assert a.qualifying_min(p, "bid", b) == p.venue_min  # depth already above target


def test_pm_us_geometric_discount_walks_out(fake_client):
    a = make("pm_us", fake_client)
    p = asyncio.run(a.programs())[0]
    book = Book([Level(0.50, 100), Level(0.48, 100), Level(0.40, 1000)], [])
    d, _ = a.scored_depth(p, "bid", book)  # target 7500 not reached: all levels count
    assert d == pytest.approx(100 * 1 + 100 * 0.3**2 + 1000 * 0.3**10)


def test_kalshi_orderbook_and_scoring(fake_client):
    a = make("kalshi", fake_client)
    p = asyncio.run(a.programs())[0]
    assert (p.target_size, p.discount, p.two_sided) == (1000.0, 0.5, True)
    assert p.R_usd_hr == pytest.approx(100 / 24)
    b = asyncio.run(a.book(p.market_id))
    assert b.bids[0].price == pytest.approx(0.88) and b.asks[0].price == pytest.approx(0.89)
    assert asyncio.run(a.book(p.market_id)).asks[1].price == pytest.approx(0.90)
    d, raw = a.scored_depth(p, "bid", b)
    assert d == pytest.approx(1000.0) and raw == pytest.approx(1255.65)
    trades = asyncio.run(a.trades(p.market_id, 0))
    assert [t.ts_ns for t in trades] == sorted(t.ts_ns for t in trades)
    assert any(t.price == pytest.approx(0.89) and t.aggressor == "buy" for t in trades)
    assert any(t.size == pytest.approx(5.57) for t in trades)


def test_kalshi_reference_price_shifts_discount(fake_client):
    a = make("kalshi", fake_client)
    p = asyncio.run(a.programs())[0]  # target 1000 -> reference at cumulative 200
    book = Book([Level(0.90, 100), Level(0.89, 300), Level(0.88, 1000)], [])
    d, _ = a.scored_depth(p, "bid", book)
    # ref=0.89 (cum 400>=200); take 100@0.90 (w1), 300@0.89 (w1), 600@0.88 (1 tick below ref)
    assert d == pytest.approx(100 + 300 + 600 * 0.5)


def test_gemini_scoring_and_pool(fake_client):
    a = make("gemini", fake_client)
    progs = asyncio.run(a.programs())
    p = progs[0]
    assert p.max_spread == pytest.approx(0.10) and p.venue_min == 10 and p.target_size is None
    assert p.maker_rebate == 0.0  # fee/rebate netting unverified -> conservative
    b = asyncio.run(a.book(p.market_id))
    d, raw = a.scored_depth(p, "bid", b)
    w = lambda x: (1 - x / 0.10) ** 2
    assert d == pytest.approx(w(0.015) * 3050 + w(0.035) * 984 + w(0.045) * 250)
    assert raw == 3050
    assert a.expected_reward(p, 10, "bid", Book([], [])) == 0.0
    t = asyncio.run(a.trades(p.market_id, 0))
    assert t[0].aggressor == "buy" and t[0].price == pytest.approx(0.63)


def test_kalshi_trades_paginate_and_dedupe(fake_client):
    a = make("kalshi", fake_client)
    one = asyncio.run(a.trades("KXHORMUZWEEKLY-26OCT04-T20", 0))
    pages = [c for c in a.c.calls if "/markets/trades" in c[0]]
    assert len(pages) == 6 and pages[1][1]["cursor"]  # follows the cursor up to trade_pages
    assert len({(t.ts_ns, t.price, t.size) for t in one}) == len(
        one
    )  # same page x6 -> deduped by id


def test_pm_us_book_exposes_shares_traded_and_price_history(fake_client):
    a = make("pm_us", fake_client)
    b = asyncio.run(a.book("aachc-cfb-2026-11-28-mostpasstds-arcman"))
    assert b.shares_traded == pytest.approx(5090.92) and b.opened_ns
    fine, coarse = asyncio.run(a.price_history("aachc-cfb-2026-11-28-mostrecs-camcole"))
    assert len(fine) == 40 and len(coarse) == 20
    assert fine[0][1] == pytest.approx((0.01 + 1 - 0.99) / 2)  # (ask + bid) / 2
    assert [t for t, _ in fine] == sorted(t for t, _ in fine)


@pytest.mark.parametrize(
    ("texts", "cat", "jump"),
    [
        (
            ("Will there be more than 20 transit calls through the Strait of Hormuz",),
            "geopolitics",
            "high",
        ),
        (("Will Brent crude close above $90",), "energy", "normal"),
        (("Will Trump sign the bill",), "politics", "high"),
        (("Will gold close above 3000",), "metals", "normal"),
        (("Highest temperature in Miami today",), "other", "normal"),
        (("Will the Fed announce a rate cut",), "rates", "high"),  # news-driven wording
    ],
)
def test_taxonomy_and_jump_class(texts, cat, jump):
    from bmm.scan.venues.common import classify, jump_class

    got = classify(*texts)
    assert got == cat and jump_class(got, *texts) == jump


def test_kalshi_price_history_uses_candle_bid_ask_mid_and_caches_series(fake_client):
    import json

    from .conftest import FIX

    a = make("kalshi", fake_client)
    fine, coarse = asyncio.run(a.price_history("KXHORMUZWEEKLY-26OCT04-T20"))
    assert a.history_source == "candles" and fine and coarse
    assert all(0 < m < 1 for _, m in fine + coarse) and [t for t, _ in fine] == sorted(
        t for t, _ in fine
    )
    c = json.loads((FIX / "kalshi/candles_60m.json").read_text())["candlesticks"]
    first = next(
        x for x in c if x["yes_bid"].get("close_dollars") and x["yes_ask"].get("close_dollars")
    )
    want = (float(first["yes_bid"]["close_dollars"]) + float(first["yes_ask"]["close_dollars"])) / 2
    assert coarse[0][1] == pytest.approx(want)
    n_events = sum("/events/" in u for u, _ in a.c.calls)
    asyncio.run(a.price_history("KXHORMUZWEEKLY-26OCT04-T20"))
    assert sum("/events/" in u for u, _ in a.c.calls) == n_events  # series ticker cached


def test_kalshi_trades_resume_returns_and_follows_cursor(fake_client):
    from bmm.scan.config import VenueCfg

    cfg = VenueCfg("kalshi", {**CFG.venue["kalshi"].raw, "trade_pages": 1})
    a = ADAPTERS["kalshi"](fake_client("kalshi"), cfg)
    trades, cur = asyncio.run(a.trades_resume("T", 0, None))
    assert trades and cur  # page cap reached with older pages still behind it
    asyncio.run(a.trades_resume("T", 0, cur))
    assert a.c.calls[-1][1]["cursor"] == cur

    class Done(fake_client):
        async def get_json(self, url, params=None):
            return {"trades": [], "cursor": ""}

    b = ADAPTERS["kalshi"](Done("kalshi"), cfg)
    assert asyncio.run(b.trades_resume("T", 0, cur)) == ([], "")


def test_program_schedules_come_from_venue_times(fake_client):
    from bmm.scan.schedule import reward_hours
    from bmm.scan.venues.common import iso_ns

    pm = asyncio.run(make("pm_us", fake_client).programs())[0]
    assert pm.start_ns == iso_ns("2026-10-02T00:00:00Z") and pm.end_ns is None and pm.pool_usd == 36
    assert reward_hours(pm, iso_ns("2026-10-04T00:00:00Z")) == pytest.approx(
        24.0
    )  # open-ended daily
    k = asyncio.run(make("kalshi", fake_client).programs())[0]
    assert (k.start_ns, k.end_ns) == (
        iso_ns("2026-10-04T18:16:35.388521Z"),
        iso_ns("2026-10-05T18:16:35.388521Z"),
    )
    assert reward_hours(k, iso_ns("2026-10-05T06:16:35.388521Z")) == pytest.approx(
        12.0
    )  # capped at time left
    g = asyncio.run(make("gemini", fake_client).programs())[0]
    assert g.end_ns == iso_ns("2026-10-06T01:30:00Z") or g.end_ns == iso_ns("2026-10-06T05:00:00Z")
    assert reward_hours(g, g.end_ns - 3600 * 10**9) == pytest.approx(1.0)
