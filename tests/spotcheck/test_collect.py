import asyncio
from dataclasses import replace
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from bmm.clock import ct_to_ns
from bmm.spotcheck.collect import collect, next_snapshot_ns
from bmm.spotcheck.contracts import PRODUCTS
from bmm.spotcheck.fixture import FixtureSource
from bmm.spotcheck.schema import ParquetSink
from bmm.spotcheck.source import SourceError

D = date(2026, 10, 5)
FIX = ct_to_ns(D, 15, 0)
SESSION = Path(__file__).resolve().parents[2] / "tests/fixtures/spotcheck/session.json"


def ct(h, m, s=0):
    return ct_to_ns(D, h, m, s)


@pytest.mark.parametrize(
    ("now", "want"),
    [(ct(10, 2), ct(10, 5)), (ct(14, 31, 10), ct(14, 32)), (ct(15, 29, 30), ct(15, 30)),
     (ct(15, 30, 1), None), (ct(10, 5), ct(10, 5)), (ct(14, 30), ct(14, 30)),
     (ct(14, 30, 1), ct(14, 31))],
)  # fmt: skip
def test_next_snapshot_ns(now, want):
    assert next_snapshot_ns(now, FIX) == want


class VClock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += round(s * 1e9)


def go(source_wrap=None, products=None, tmp=None):
    vc = VClock(ct(11, 30))
    src = FixtureSource.from_json(SESSION, clock=vc)
    if source_wrap:
        src = source_wrap(src)
    sink = ParquetSink(tmp)
    prods = products or list(PRODUCTS.values())
    status = asyncio.run(collect(src, prods, D, sink, vc, vc.sleep))
    return src, status


def test_dry_run_all_products_ok(tmp_path):
    import time

    t0 = time.time()
    src, status = go(tmp=tmp_path)
    assert time.time() - t0 < 10
    assert status == dict.fromkeys(PRODUCTS, "ok")
    day = tmp_path / "date=2026-10-05"
    lad, fut, opt = (pl.read_parquet(day / f"{n}.parquet") for n in ("ladder", "futures", "options"))
    assert (day / "contracts.json").exists()
    for key, p in src.session.products.items():
        sub = lad.filter(pl.col("product") == key)
        per = sub.group_by("snapshot_id").len()["len"]
        assert set(per) == {len(p.ladder_strikes)}, key
        assert sub["snapshot_id"].n_unique() == len(p.snaps), key
    spx = opt.filter(pl.col("product") == "forecastex-spx")
    assert spx.height > 0 and not spx["expiry_mismatch"].any()
    assert (spx["expiry_role"] == "match").all()
    cme = opt.filter(pl.col("product") == "cme-gold")
    assert cme.height > 0 and cme["expiry_mismatch"].all()
    assert set(cme["expiry_role"]) == {"above"}
    # post-fix: ladder rows exist but a matched product has no option rows after its expiry
    fix_ts = ct_to_ns(D, 15, 0)
    assert lad.filter((pl.col("product") == "forecastex-spx") & (pl.col("ts_ns") > fix_ts)).height > 0
    assert spx.filter(pl.col("ts_ns") >= fix_ts).height == 0
    assert lad.filter(pl.col("crossed")).height >= 4
    assert fut.filter(pl.col("product") == "cme-gold").height == len(src.session.products["cme-gold"].snaps)
    # ladder strikes are covered by the option window (bracketing)
    snap = lad.filter(pl.col("product") == "forecastex-spx")["snapshot_id"][0]
    ks = lad.filter(pl.col("snapshot_id") == snap)["strike"]
    os_ = opt.filter(pl.col("snapshot_id") == snap)["strike"]
    assert os_.min() <= ks.min() and os_.max() >= ks.max()


class FailGold:
    def __init__(self, inner, exc=None):
        self.inner, self.exc = inner, exc or SourceError("no subscription")

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def ladder(self, spec, qd):
        if spec.key == "forecastex-gold":
            raise self.exc
        return await self.inner.ladder(spec, qd)


def test_product_failure_isolated(tmp_path):
    _, status = go(FailGold, tmp=tmp_path)
    assert status["forecastex-gold"].startswith("dropped after 3 consecutive failures")
    assert "no subscription" in status["forecastex-gold"]
    assert [status[k] for k in ("forecastex-spx", "forecastex-ndx", "cme-gold")] == ["ok"] * 3
    lad = pl.read_parquet(tmp_path / "date=2026-10-05/ladder.parquet")
    assert "forecastex-gold" not in set(lad["product"])


def test_connection_and_timeout_errors_are_isolated(tmp_path):
    for exc in (ConnectionError("gw down"), TimeoutError("slow")):
        _, status = go(lambda s, e=exc: FailGold(s, e), tmp=tmp_path / type(exc).__name__)
        assert "dropped" in status["forecastex-gold"] and status["cme-gold"] == "ok"


def test_scale_mismatch_is_a_failure_not_a_crash(tmp_path):
    bad = replace(PRODUCTS["cme-gold"], payout_scale=0.01)
    prods = [PRODUCTS["forecastex-spx"], bad]
    _, status = go(products=prods, tmp=tmp_path)
    assert status["forecastex-spx"] == "ok"
    assert "ScaleError" in status["cme-gold"]


def test_scale_too_large_is_caught_at_ladder_level(tmp_path):
    bad = replace(PRODUCTS["cme-gold"], payout_scale=100.0)
    _, status = go(products=[bad], tmp=tmp_path)
    assert "ScaleError" in status["cme-gold"] and "too large" in status["cme-gold"]


def test_no_options_for_reference_future_still_writes_ladder_and_futures(tmp_path):
    class NoOpts:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def option_expiries(self, spec, fut):
            return []

    _, status = go(NoOpts, products=[PRODUCTS["forecastex-ndx"]], tmp=tmp_path)
    assert status == {"forecastex-ndx": "ok"}
    day = tmp_path / "date=2026-10-05"
    assert (day / "ladder.parquet").exists() and (day / "futures.parquet").exists()
    assert not (day / "options.parquet").exists()


def test_contracts_logged_once_with_assumption_note(tmp_path):
    import json

    go(products=[PRODUCTS["forecastex-ndx"]], tmp=tmp_path)
    data = json.loads((tmp_path / "date=2026-10-05/contracts.json").read_text())
    assert any("assumption" in d for d in data["forecastex-ndx"])
    assert data["forecastex-ndx"][0]["lastTradeTime"] == "15:00:00"


def test_clock_past_window_takes_no_snapshots(tmp_path):
    vc = VClock(ct(16, 0))
    src = FixtureSource.from_json(SESSION, clock=vc)
    status = asyncio.run(collect(src, [PRODUCTS["forecastex-spx"]], D, ParquetSink(tmp_path), vc, vc.sleep))
    assert status["forecastex-spx"].startswith("no snapshots")
