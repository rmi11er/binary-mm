import asyncio
import logging

import polars as pl
import pytest

from bmm.scan.metrics import eval_coll, exclusion, fills_contracts_hr, markout_30m, side_of
from bmm.scan.models import Book, Level, MarketMeta, Program, Trade, VenueNotPublic
from bmm.scan.runner import snapshot_once

H = 3600 * 10**9
NOW = 1_800_000_000 * 10**9
MIDS = pl.DataFrame({"ts_ns": [0], "mid": [0.5]})


def meta(**kw):
    d = {
        "market_id": "m", "title": "t", "category": "other", "expiry_ns": NOW + 100 * H,
        "settle_source": None, "settle_ns": None, "reference_quality": "none", "in_game": False,
    }  # fmt: skip
    return MarketMeta(**(d | kw))


BOOK = Book([Level(0.49, 10)], [Level(0.51, 10)])
BOOK_HOLDER_60 = Book([Level(0.49, 10, 0.6)], [Level(0.51, 10, 0.1)])
BOOK_HOLDER_UNKNOWN = Book([Level(0.49, 10, None)], [Level(0.51, 10, None)])


def test_fill_rate_counts_contracts_not_trades():
    t = [Trade(0, 0.40, 7.0, "sell"), Trade(1, 0.40, 3.0, "sell"), Trade(2, 0.60, 5.0, "buy")]
    assert fills_contracts_hr(t, "bid", MIDS, H) == pytest.approx(10.0)
    assert fills_contracts_hr(t, "ask", MIDS, H) == pytest.approx(5.0)


def test_markout_maker_side_sign_and_size_weight():
    t = pl.DataFrame(
        {"ts_ns": [0, 0], "price": [0.40, 0.40], "size": [1.0, 3.0], "aggressor": ["sell", "sell"]}
    )
    m = pl.DataFrame({"ts_ns": [30 * 60 * 10**9], "mid": [0.30]})
    val, n = markout_30m(t, m, "bid")  # maker bought at .40, mid -> .30: -0.10 each
    assert val == pytest.approx(-0.10) and n == 2
    t2 = pl.DataFrame({"ts_ns": [0], "price": [0.60], "size": [2.0], "aggressor": ["buy"]})
    m2 = pl.DataFrame({"ts_ns": [30 * 60 * 10**9], "mid": [0.70]})
    assert markout_30m(t2, m2, "ask")[0] == pytest.approx(-0.10)  # maker sold at .60, mid .70


def test_markout_none_without_history():
    t = pl.DataFrame({"ts_ns": [0], "price": [0.4], "size": [1.0], "aggressor": ["sell"]})
    assert markout_30m(t, pl.DataFrame({"ts_ns": [], "mid": []}), "bid") == (None, 0)


def test_unknown_aggressor_uses_tick_rule():
    assert side_of(Trade(0, 0.45, 1.0, None), 0.50) == "bid"
    assert side_of(Trade(0, 0.50, 1.0, None), 0.50) is None


@pytest.mark.parametrize(
    ("m", "book", "reason"),
    [
        (meta(in_game=True), BOOK, "live in-game"),
        (meta(category="crypto", expiry_ns=NOW + 12 * H), BOOK, "crypto <1d to expiry"),
        (meta(), BOOK_HOLDER_60, "one participant >50% at touch"),
        (meta(), BOOK_HOLDER_UNKNOWN, None),  # unknown concentration is NOT excluded
        (meta(category="crypto", expiry_ns=NOW + 48 * H), BOOK, None),
    ],
)
def test_exclusion(m, book, reason):
    assert exclusion(m, book, NOW) == reason


@pytest.mark.parametrize(
    ("expr", "p", "want"),
    [
        ("p", 0.4, 0.4),
        ("1-p", 0.4, 0.6),
        ("[?]", 0.4, 0.4),
        (None, 0.4, 0.4),
        ("max(p, 1-p)", 0.4, 0.6),
    ],
)
def test_eval_coll(expr, p, want):
    assert eval_coll(expr, p) == pytest.approx(want)


def test_eval_coll_rejects_code():
    with pytest.raises(ValueError):
        eval_coll("__import__('os').system('x')", 0.4)


PROG = Program("v", "m", 12.0, 100.0, 0.05, 10.0, None, "pro_rata_quadratic", True, 0.0, 0.0, "h")


class Fake:
    def __init__(self, name, fail=False):
        self.name, self.fail = name, fail

    async def programs(self):
        if self.fail:
            raise VenueNotPublic("needs credentials")
        return [PROG]

    async def meta(self, market_id):
        return meta(market_id=market_id)

    async def book(self, market_id):
        return BOOK

    async def trades(self, market_id, since_ns):
        return [Trade(NOW - H, 0.49, 4.0, "sell"), Trade(NOW - H, 0.51, 6.0, "buy")]

    def scored_depth(self, prog, side, book):
        return 20.0, 10.0

    def expected_reward(self, prog, s, side, book):
        return 0.0

    def qualifying_min(self, prog, side, book):
        return 10

    def s_star_closed_form(self, prog, side, book, c):
        return None


def test_snapshot_skips_non_public_venue_and_counts_contracts(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    out = asyncio.run(snapshot_once([Fake("bad", fail=True), Fake("v")], NOW, root=tmp_path))
    rows = out["snapshots"]
    assert {r["venue"] for r in rows} == {"v"} and {r["side"] for r in rows} == {"bid", "ask"}
    assert "needs credentials" in caplog.text
    bid = next(r for r in rows if r["side"] == "bid")
    assert bid["fills_side_hr"] == pytest.approx(4.0 / 24)  # 24h window, contracts not trades
    assert bid["n_trades_side"] == 1 and bid["markout_30m_side"] is None
    assert {"programs", "trades", "books"} <= set(out)


def test_snapshot_scales_fee_to_p1p_and_flags_no_tape(tmp_path):
    from bmm.scan.config import VenueCfg

    class Fee(Fake):
        async def programs(self):
            return [PROG.__class__(**{**PROG.__dict__, "maker_fee": 0.02, "maker_rebate": 0.0125})]

    cfg = {"v": VenueCfg("v", {"trades_aggressor": "none"})}
    rows = asyncio.run(snapshot_once([Fee("v")], NOW, cfgs=cfg, root=tmp_path))["snapshots"]
    assert all(r["fills_lower_bound"] for r in rows)
    assert rows[0]["maker_fee"] == pytest.approx(0.02 * 0.5 * 0.5)  # mid = 0.50
    assert rows[0]["maker_rebate"] == pytest.approx(0.0125 * 0.25)
