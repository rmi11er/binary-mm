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
    assert trades and trades[0].price == pytest.approx(0.89) and trades[0].aggressor == "buy"
    assert trades[0].size == pytest.approx(5.57)


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
