import asyncio

import pytest

from bmm.scan import store
from bmm.scan.metrics import H_NS
from bmm.scan.models import Book, Level, MarketMeta, Program, Trade
from bmm.scan.runner import due_full, snapshot_once

NOW = 1_800_000_000 * 10**9
DAY = 24 * H_NS
PROG = Program("v", "m", 12.0, 100.0, 0.05, 5.0, None, "x", False, 0.0, 0.0, "h")
META = MarketMeta("m", "t", "politics", NOW + 10 * DAY, None, None, "none", False, "high")


class A:
    name = "v"

    def __init__(self, shares=None, opened=None, history=True, tape=True):
        self.shares, self.opened, self.history, self.tape = shares, opened, history, tape
        self.programs_calls = self.meta_calls = 0

    async def programs(self):
        self.programs_calls += 1
        return [PROG]

    async def meta(self, market_id):
        self.meta_calls += 1
        return META

    async def book(self, market_id):
        return Book([Level(0.49, 10)], [Level(0.51, 10)], self.shares, self.opened)

    async def trades(self, market_id, since_ns):
        if not self.tape:
            return []
        # one fill a day for 7 days: sells hit our bid; mid drifts adversely 30m later
        return [Trade(NOW - k * DAY, 0.49, 2.0, "sell") for k in range(1, 7)] + [
            Trade(NOW - k * DAY + 60 * 10**9, 0.51, 1.0, "buy") for k in range(1, 7)
        ]

    async def price_history(self, market_id):
        fine = [(NOW - DAY + i * 300 * 10**9, 0.5 + (0.1 if i > 140 else 0.0)) for i in range(288)]
        return fine, [(NOW - 7 * DAY + i * 3 * H_NS, 0.5) for i in range(56)]

    def scored_depth(self, prog, side, book):
        return 20.0, 10.0


def rows(out, side="bid"):
    return next(r for r in out["snapshots"] if r["side"] == side)


def test_backfill_populates_proxied_fields_and_stores_series(tmp_path):
    out = asyncio.run(snapshot_once([A()], NOW, root=tmp_path))
    r = rows(out)
    assert out["trades"] and len(out["trades"]) == 12  # 7d of tape fetched on first sight
    assert out["proxy_mids"]  # series stored for later scans
    assert r["vol30_proxy"] is not None and r["vol30_proxy"] > 0
    assert r["jump_p99_1h"] is not None and r["jump_class"] == "high"
    assert r["n_markout_side"] == 0  # no organic mids yet -> nothing organic
    assert r["n_markout_proxied_side"] >= 0 and "markout_proxied_side" in r


def test_trade_derived_series_when_adapter_has_no_history(tmp_path):
    class NoHist(A):
        price_history = None  # e.g. Kalshi: the series comes from the trade tape

    out = asyncio.run(snapshot_once([NoHist()], NOW, root=tmp_path))
    assert out["proxy_mids"] and rows(out)["fills_proxied"] is False


def test_pm_fills_estimated_from_shares_traded(tmp_path):
    a = A(shares=240.0, opened=NOW - 24 * H_NS, tape=False)
    r = rows(asyncio.run(snapshot_once([a], NOW, root=tmp_path)))
    assert r["fills_side_hr"] == pytest.approx(240 / 24 / 2)  # lifetime avg, half per side
    assert r["fills_proxied"] is True and r["fills_lower_bound"] is False
    # second scan 2h later: delta since first snapshot
    store.write("snapshots", [r | {"shares_traded": 240.0}], NOW, tmp_path)
    b = A(shares=300.0, opened=NOW - 24 * H_NS, tape=False)
    r2 = rows(asyncio.run(snapshot_once([b], NOW + 2 * H_NS, root=tmp_path)))
    assert r2["fills_side_hr"] == pytest.approx(60 / 2 / 2)


def test_fast_tier_reuses_stored_terms_and_meta(tmp_path):
    full = asyncio.run(snapshot_once([A()], NOW, root=tmp_path))
    for t, rws in full.items():
        store.write(t, rws, NOW, tmp_path)
    a = A()
    out = asyncio.run(snapshot_once([a], NOW + 300 * 10**9, root=tmp_path, only={("v", "m")}))
    assert a.programs_calls == 0 and a.meta_calls == 0  # no terms/universe refetch
    assert {r["side"] for r in out["snapshots"]} == {"bid", "ask"}
    assert rows(out)["jump_class"] == "high" and rows(out)["category"] == "politics"
    assert out["programs"] == []  # terms are only (re)written by the hourly tier


def test_due_full():
    assert due_full(None, NOW)
    assert not due_full(NOW - 59 * 60 * 10**9, NOW)
    assert due_full(NOW - 61 * 60 * 10**9, NOW)


def test_venues_scan_concurrently(tmp_path):
    import time

    class Slow(A):
        async def book(self, market_id):
            await asyncio.sleep(0.3)
            return await super().book(market_id)

    a, b = Slow(), Slow()
    b.name = "w"
    t0 = time.monotonic()
    asyncio.run(snapshot_once([a, b], NOW, root=tmp_path))
    assert time.monotonic() - t0 < 0.55  # two venues overlap instead of 0.6s serial


def test_mid_source_and_weak_proxy_flag(tmp_path):
    class NoHist(A):
        price_history = None

    weak = rows(asyncio.run(snapshot_once([NoHist()], NOW, root=tmp_path / "w")))
    assert weak["mid_source"] == "trade_buysell" and weak["weak_proxy"] is True
    strong = rows(asyncio.run(snapshot_once([A()], NOW, root=tmp_path / "s")))
    assert strong["mid_source"] == "price_history" and strong["weak_proxy"] is False


class Pre(A):
    async def trades(self, market_id, since_ns):
        return [
            Trade(NOW - 3 * H_NS, 0.49, 2.0, "sell"),
            Trade(NOW - 105 * 60 * 10**9, 0.49, 3.0, "sell"),
        ]

    async def price_history(self, market_id):
        return [(NOW - DAY + i * 300 * 10**9, 0.45) for i in range(289)], []


def test_proxied_prior_uses_only_fills_before_first_organic_snapshot(tmp_path):
    first = {"ts_ns": NOW - 2 * H_NS, "venue": "v", "market_id": "m", "side": "bid", "mid": 0.5}
    store.write("snapshots", [first], NOW - 2 * H_NS, tmp_path)
    r = rows(asyncio.run(snapshot_once([Pre()], NOW, root=tmp_path)))
    assert (
        r["n_markout_proxied_side"] == 1
    )  # only the fill 3h ago predates the first organic snapshot
    assert r["markout_proxied_side"] == pytest.approx(0.45 - 0.49)
    assert r["n_markout_side"] == 0


class K(A):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def trades_resume(self, market_id, since_ns, cursor):
        self.calls.append((since_ns, cursor))
        if len(self.calls) == 1:
            return [
                Trade(NOW - DAY, 0.49, 1.0, "sell"),
                Trade(NOW - 2 * H_NS, 0.49, 1.0, "sell"),
            ], "c1"
        return [], ""


def test_kalshi_backfill_stores_cursor_and_resumes_incrementally(tmp_path):
    k = K()
    out = asyncio.run(snapshot_once([k], NOW, root=tmp_path))
    assert out["cursors"] == [
        {"venue": "v", "market_id": "m", "cursor": "c1", "min_ts": NOW - 7 * DAY, "ts_ns": NOW}
    ]
    for t, rws in out.items():
        store.write(t, rws, NOW, tmp_path)
    k2 = K()
    k2.calls = [(0, None)]  # make the fake return the "no more pages" answer from now on
    later = NOW + H_NS
    out2 = asyncio.run(snapshot_once([k2], later, root=tmp_path))
    assert k2.calls[1:] == [
        (NOW - 2 * H_NS + 1, None),
        (NOW - 7 * DAY, "c1"),  # replayed with the ORIGINAL min_ts
    ]  # newest first, then older pages
    assert out2["cursors"][0]["cursor"] == ""  # finished: nothing to resume next time


def test_old_trade_derived_proxy_rows_count_as_weak(tmp_path):
    rows_ = [{"venue": "v", "market_id": "m", "ts_ns": NOW - DAY + i * 300 * 10**9, "mid": 0.5, "kind": "fine",
              "source": "trade_derived"} for i in range(200)]  # fmt: skip
    store.write("proxy_mids", rows_, NOW, tmp_path)
    r = rows(asyncio.run(snapshot_once([A()], NOW, root=tmp_path)))
    assert r["weak_proxy"] is True and r["mid_source"] == "trade_derived"


class G(A):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def trades_resume(self, market_id, since_ns, cursor):
        self.calls.append((since_ns, cursor))
        if cursor == "g1":
            return [Trade(NOW + 100 * 10**9, 0.49, 1.0, "sell")], ""
        if cursor is None and len(self.calls) == 1:
            return [Trade(NOW - 2 * H_NS, 0.49, 1.0, "sell")], ""
        if cursor is None:
            return [
                Trade(NOW + 200 * 10**9, 0.49, 1.0, "sell")
            ], "g1"  # more than one page since last scan
        return [], ""


def test_gap_since_last_scan_is_paged_until_closed(tmp_path):
    g = G()
    for t, rws in asyncio.run(snapshot_once([g], NOW, root=tmp_path)).items():
        store.write(t, rws, NOW, tmp_path)
    out = asyncio.run(snapshot_once([g], NOW + H_NS, root=tmp_path))
    assert (NOW - 2 * H_NS + 1, "g1") in g.calls  # followed the cursor of the new-trades fetch
    assert len(out["trades"]) == 2  # both pages kept
