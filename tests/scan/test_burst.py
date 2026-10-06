import asyncio

import pytest

from bmm.scan import store
from bmm.scan.burst import run_burst
from bmm.scan.models import Book, Level

S = 10**9


class Clk:
    def __init__(self):
        self.t = 0

    def now(self):
        return self.t

    async def sleep(self, d):
        self.t += int(d * S)


class Ad:
    name = "v"

    def __init__(self):
        self.n = 0

    async def book(self, market_id):
        self.n += 1
        return Book([Level(0.49 + 0.01 * (self.n // 3), 10.0 + self.n)], [Level(0.51, 20.0)])


def test_burst_samples_the_touch_at_the_requested_cadence(tmp_path):
    c, a = Clk(), Ad()
    rows = asyncio.run(
        run_burst(
            {"v": a}, [("v", "m")], seconds=10, every=2, root=tmp_path, now=c.now, sleep=c.sleep
        )
    )
    assert len(rows) == 5 and [r["ts_ns"] for r in rows] == [0, 2 * S, 4 * S, 6 * S, 8 * S]
    assert rows[0]["bid"] == 0.49 and rows[0]["ask"] == 0.51 and rows[0]["bid_size"] == 11.0
    assert not store.read("bursts", None, tmp_path).is_empty()  # persisted


def test_burst_skips_a_market_that_errors_and_keeps_sampling(tmp_path):
    class Bad(Ad):
        async def book(self, market_id):
            raise KeyError("shape")

    c = Clk()
    rows = asyncio.run(
        run_burst(
            {"v": Bad()}, [("v", "m")], seconds=4, every=2, root=tmp_path, now=c.now, sleep=c.sleep
        )
    )
    assert rows == []


def test_runner_rows_carry_tier_touch_sizes_reference_and_taker_fee(tmp_path):
    from bmm.scan.config import VenueCfg
    from bmm.scan.models import MarketMeta
    from bmm.scan.runner import snapshot_once

    from .test_runner_tiers import NOW, A

    class RefA(A):
        async def meta(self, market_id):
            return MarketMeta(market_id, "t", "other", None, None, None, "venue_ref", False)

        async def reference_fair(self, market_id):
            return 0.5

    cfg = {"v": VenueCfg("v", {"taker_fee": 0.0695, "taker_fee_verified": True})}
    r = asyncio.run(snapshot_once([RefA()], NOW, cfgs=cfg, root=tmp_path))["snapshots"][0]
    assert (r["tier"], r["bid_size"], r["ask_size"], r["ref_fair"], r["taker_fee_coeff"]) == (
        "full",
        10.0,
        10.0,
        0.5,
        0.0695,
    )
    plain = asyncio.run(snapshot_once([A()], NOW, cfgs=cfg, root=tmp_path / "p"))["snapshots"][0]
    assert plain["ref_fair"] is None  # no venue_ref -> the hook is never consulted
    assert pytest.approx(plain["bid_size"]) == 10.0


def test_unverified_taker_fee_is_not_used(tmp_path):
    from bmm.scan.config import VenueCfg
    from bmm.scan.runner import snapshot_once

    from .test_runner_tiers import NOW, A

    cfg = {"v": VenueCfg("v", {"taker_fee": 0.07, "taker_fee_verified": False})}
    r = asyncio.run(snapshot_once([A()], NOW, cfgs=cfg, root=tmp_path))["snapshots"][0]
    assert r["taker_fee_coeff"] is None  # never score on a guessed fee
