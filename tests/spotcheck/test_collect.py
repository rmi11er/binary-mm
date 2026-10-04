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
    lad, fut, opt = (
        pl.read_parquet(day / f"{n}.parquet") for n in ("ladder", "futures", "options")
    )
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
    assert (
        lad.filter((pl.col("product") == "forecastex-spx") & (pl.col("ts_ns") > fix_ts)).height > 0
    )
    assert spx.filter(pl.col("ts_ns") >= fix_ts).height == 0
    assert lad.filter(pl.col("crossed")).height >= 4
    assert fut.filter(pl.col("product") == "cme-gold").height == len(
        src.session.products["cme-gold"].snaps
    )
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


def test_unexpected_exception_isolated(tmp_path):
    for exc in (ValueError("bad"), KeyError("k"), IndexError("i"), AttributeError("a")):
        _, status = go(lambda s, e=exc: FailGold(s, e), tmp=tmp_path / type(exc).__name__)
        assert "dropped" in status["forecastex-gold"], exc
        assert type(exc).__name__ in status["forecastex-gold"]
        assert [status[k] for k in ("forecastex-spx", "forecastex-ndx", "cme-gold")] == ["ok"] * 3


def test_safety_error_not_swallowed(tmp_path):
    from bmm.spotcheck.safety import SafetyError

    with pytest.raises(SafetyError):
        go(lambda s: FailGold(s, SafetyError("non-paper")), tmp=tmp_path)


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

        async def option_expiries(self, spec, fut, question_date=None):
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
    status = asyncio.run(
        collect(src, [PRODUCTS["forecastex-spx"]], D, ParquetSink(tmp_path), vc, vc.sleep)
    )
    assert status["forecastex-spx"].startswith("no snapshots")


# --- quant-review fixes ------------------------------------------------------------------------
def _read(tmp, name):
    return pl.read_parquet(tmp / "date=2026-10-05" / f"{name}.parquet")


def test_event_contract_class_never_chosen_cme_gold_all_mismatched(tmp_path):
    src, status = go(products=[PRODUCTS["cme-gold"]], tmp=tmp_path)
    assert status == {"cme-gold": "ok"}
    # the fixture lists a decoy ECGC expiry exactly at the fix
    decoys = [e for e in src.session.products["cme-gold"].expiries if e["trading_class"] == "ECGC"]
    assert len(decoys) == 1 and decoys[0]["expiry_ts_ns"] == ct(12, 30)
    opt = _read(tmp_path, "options")
    assert set(opt["trading_class"]) == {"OG"}
    assert opt["expiry_mismatch"].all() and set(opt["expiry_role"]) == {"above"}


def test_collector_filters_ec_class_even_if_source_returns_it(tmp_path):
    class Decoy:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def option_expiries(self, spec, fut, question_date=None):
            got = await self.inner.option_expiries(spec, fut, question_date)
            return [e for e in got if e.trading_class == "ECGC"]  # ONLY the event class

    _, status = go(Decoy, products=[PRODUCTS["cme-gold"]], tmp=tmp_path)
    assert status == {"cme-gold": "ok"}
    assert not (tmp_path / "date=2026-10-05/options.parquet").exists()


def test_options_table_has_trading_class_sizes_and_times(tmp_path):
    go(products=[PRODUCTS["forecastex-spx"]], tmp=tmp_path)
    opt = _read(tmp_path, "options")
    assert set(opt["trading_class"]) == {"ES"}
    assert opt["call_bid_size"].null_count() == 0 and (opt["call_bid_size"] == 10).all()
    assert (opt["opt_ts_ns"] >= opt["ts_ns"]).all()


class MovingFuture:
    """Second and later reference_future calls in a snapshot return F + move."""

    def __init__(self, inner, move):
        self.inner, self.move, self.n = inner, move, {}

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def reference_future(self, spec, qd, details, gc):
        from dataclasses import replace

        f = await self.inner.reference_future(spec, qd, details, gc)
        k = (spec.key, self.inner._clock())
        self.n[k] = self.n.get(k, 0) + 1
        if self.n[k] == 1:
            return f
        q = f.quote
        return replace(f, quote=replace(q, bid=q.bid + self.move, ask=q.ask + self.move))


def test_future_requoted_after_options_und_px_is_mean_and_df_flag(tmp_path):
    _, status = go(lambda s: MovingFuture(s, 0.2), products=[PRODUCTS["forecastex-spx"]],
                   tmp=tmp_path)  # fmt: skip
    assert status == {"forecastex-spx": "ok"}
    fut, opt = _read(tmp_path, "futures"), _read(tmp_path, "options")
    pre = fut.filter(pl.col("ts_ns") < FIX)
    assert (pre["und_px_after"] - pre["und_px_before"]).abs().min() == pytest.approx(0.2, abs=1e-9)
    j = opt.join(fut.select("snapshot_id", "und_px_before", "und_px_after"), on="snapshot_id")
    mean = 0.5 * (j["und_px_before"] + j["und_px_after"])
    assert (j["und_px"] - mean).abs().max() == pytest.approx(0.0, abs=1e-9)
    # digital delta ~0.019/pt at 3.5h (0.2 pt -> 0.4c, not flagged) and ~0.09/pt at 10 min (-> 1.8c)
    near = pre.filter(pl.col("ts_ns").is_between(FIX - 10 * 60 * 10**9, FIX - 5 * 60 * 10**9))
    assert near.height >= 5 and near["df_flag"].all()
    early = pre.filter(pl.col("ts_ns") < FIX - 3 * 3600 * 10**9)
    assert early.height > 0 and not early["df_flag"].any()


def test_no_df_flag_when_future_does_not_move(tmp_path):
    go(products=[PRODUCTS["forecastex-spx"]], tmp=tmp_path)
    fut = _read(tmp_path, "futures")
    assert not fut["df_flag"].any()
    assert (fut["und_px_before"] == fut["und_px_after"]).all()


def test_each_table_records_its_own_fetch_time(tmp_path):
    class Slow:
        """A clock that advances 3 s per fetch, like sequential IB requests."""

        def __init__(self, inner, vc):
            self.inner, self.vc = inner, vc

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def ladder(self, spec, qd):
            r = await self.inner.ladder(spec, qd)
            self.vc.t += 3 * 10**9
            return r

        async def reference_future(self, *a):
            r = await self.inner.reference_future(*a)
            self.vc.t += 3 * 10**9
            return r

    vc = VClock(ct(11, 30))
    inner = FixtureSource.from_json(SESSION, clock=vc)
    asyncio.run(collect(Slow(inner, vc), [PRODUCTS["forecastex-spx"]], D, ParquetSink(tmp_path),
                        vc, vc.sleep))  # fmt: skip
    lad, fut = _read(tmp_path, "ladder"), _read(tmp_path, "futures")
    first = lad.filter(pl.col("snapshot_id") == lad["snapshot_id"][0])
    assert first["ladder_ts_ns"][0] - first["ts_ns"][0] == 3 * 10**9
    f0 = fut.filter(pl.col("snapshot_id") == lad["snapshot_id"][0])
    assert f0["fut_before_ts_ns"][0] - f0["ts_ns"][0] == 6 * 10**9
    assert f0["fut_ts_ns"][0] - f0["ts_ns"][0] == 9 * 10**9  # the re-quote


def test_atm_sigma_unresolved_still_fetches_ladder_range(tmp_path):
    class NoAtm:
        def __init__(self, inner):
            self.inner, self.first = inner, {}

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def calls(self, spec, expiry, strikes):
            k = (spec.key, self.inner._clock())
            if k not in self.first:  # the ATM probe returns nothing usable
                self.first[k] = True
                return []
            return await self.inner.calls(spec, expiry, strikes)

    _, status = go(NoAtm, products=[PRODUCTS["forecastex-spx"]], tmp=tmp_path)
    assert status == {"forecastex-spx": "ok"}
    lad, opt = _read(tmp_path, "ladder"), _read(tmp_path, "options")
    sid = opt["snapshot_id"][0]
    ks = lad.filter(pl.col("snapshot_id") == sid)["strike"]
    os_ = opt.filter(pl.col("snapshot_id") == sid)["strike"]
    assert os_.min() <= ks.min() and os_.max() >= ks.max()
    assert opt["atm_sigma"].null_count() == 0  # resolved once the wider chain is in


def test_edge_strikes_re_extended_after_qualification_drops(tmp_path):
    class DropEdge:
        """Drops the lowest strike of any large request, as qualification can."""

        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def calls(self, spec, expiry, strikes):
            out = await self.inner.calls(spec, expiry, strikes)
            if len(strikes) > 5:
                lo = min(q.strike for q in out)
                return [q for q in out if q.strike != lo]
            return out

    _, status = go(DropEdge, products=[PRODUCTS["forecastex-spx"]], tmp=tmp_path)
    assert status == {"forecastex-spx": "ok"}
    lad, opt = _read(tmp_path, "ladder"), _read(tmp_path, "options")
    sid = opt["snapshot_id"][0]
    ks = lad.filter(pl.col("snapshot_id") == sid)["strike"]
    assert opt.filter(pl.col("snapshot_id") == sid)["strike"].min() <= ks.min()


def test_ladder_has_post_fix_flag_and_none_for_missing_raw_sizes(tmp_path):
    go(products=[PRODUCTS["forecastex-spx"]], tmp=tmp_path)
    lad = _read(tmp_path, "ladder")
    assert (lad["post_fix"] == (lad["ts_ns"] >= lad["fix_ts_ns"])).all()
    assert lad["post_fix"].any() and not lad["post_fix"].all()
    assert (lad["ladder_ts_ns"] >= lad["ts_ns"]).all()
    # a raw size is only logged next to a raw price
    assert lad.filter(pl.col("raw_no_ask").is_null())["raw_no_ask_size"].null_count() == (
        lad.filter(pl.col("raw_no_ask").is_null()).height
    )


# --- quant-review 11-13 nits ---------------------------------------------------------------------
def test_df_flag_delta_uses_time_to_fix_not_to_expiry(tmp_path):
    """cme-gold's chain expires 24 h after the fix; the delta must use T to the fix (~0.4/pt
    near the fix), so a 0.2 pt move flags where T-to-expiry (~0.01/pt) would not."""
    _, status = go(lambda s: MovingFuture(s, 0.2), products=[PRODUCTS["cme-gold"]], tmp=tmp_path)
    assert status == {"cme-gold": "ok"}
    fut = _read(tmp_path, "futures")
    fix = ct(12, 30)
    near = fut.filter(pl.col("ts_ns").is_between(fix - 10 * 60 * 10**9, fix - 60 * 10**9))
    assert near.height > 5 and near["df_flag"].all()


def test_post_fix_snapshots_are_not_flagged(tmp_path):
    _, _ = go(lambda s: MovingFuture(s, 5.0), products=[PRODUCTS["cme-gold"]], tmp=tmp_path)
    fut = _read(tmp_path, "futures")
    assert not fut.filter(pl.col("ts_ns") >= FIX)["df_flag"].any()


class NoSigma:
    """ATM quotes have bid 0 for the first `n_bad` snapshots, so atm_sigma cannot resolve."""

    def __init__(self, inner, n_bad_after=None):
        self.inner, self.bad_from = inner, n_bad_after

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def calls(self, spec, expiry, strikes):
        from bmm.spotcheck.source import SizedOptQuote

        out = await self.inner.calls(spec, expiry, strikes)
        if self.bad_from is None or self.inner._clock() >= self.bad_from:
            return [SizedOptQuote(q.strike, 0.0, q.call_ask) for q in out]  # bid 0: unusable IV
        return out


def test_df_flag_null_when_sigma_never_resolved(tmp_path):
    _, status = go(lambda s: NoSigma(s), products=[PRODUCTS["forecastex-spx"]], tmp=tmp_path)
    assert status == {"forecastex-spx": "ok"}
    fut = _read(tmp_path, "futures")
    pre = fut.filter(pl.col("ts_ns") < FIX)
    assert pre["df_flag"].null_count() == pre.height  # unknown, not False


def test_df_flag_uses_last_resolved_sigma_when_snapshot_sigma_missing(tmp_path):
    bad_from = ct(14, 0)
    _, status = go(lambda s: MovingFuture(NoSigma(s, bad_from), 0.2),
                   products=[PRODUCTS["forecastex-spx"]], tmp=tmp_path)  # fmt: skip
    assert status == {"forecastex-spx": "ok"}
    fut = _read(tmp_path, "futures")
    late = fut.filter(pl.col("ts_ns").is_between(bad_from, FIX - 5 * 60 * 10**9))
    assert late.height > 20
    assert late["df_flag"].null_count() == 0  # the memoised sigma sized the move
    near = late.filter(pl.col("ts_ns") >= FIX - 10 * 60 * 10**9)  # delta ~0.09/pt: 0.2 pt > 1c
    assert near.height >= 5 and near["df_flag"].all()
    assert not late.filter(pl.col("ts_ns") <= ct(14, 5))["df_flag"].any()  # ~0.02/pt: not flagged


def test_late_future_quote_flags_the_snapshot(tmp_path):
    class SlowFut:
        def __init__(self, inner, vc):
            self.inner, self.vc = inner, vc

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def reference_future(self, *a):
            self.vc.t += 6 * 10**9  # > 5 s after the ladder
            return await self.inner.reference_future(*a)

    vc = VClock(ct(11, 30))
    inner = FixtureSource.from_json(SESSION, clock=vc)
    asyncio.run(collect(SlowFut(inner, vc), [PRODUCTS["forecastex-spx"]], D,
                        ParquetSink(tmp_path), vc, vc.sleep))  # fmt: skip
    fut = _read(tmp_path, "futures").sort("ts_ns")
    pre = fut.filter(pl.col("ts_ns") < FIX)
    assert pre["df_flag"][0] is True  # first snapshot: future quoted 6 s after the ladder
    assert not pre["df_flag"][1:].any()  # cached: quoted before the ladder, no gate


# --- pre-merge part 1 ----------------------------------------------------------------------------
class Recorder:
    """Records the order of source calls for one product."""

    def __init__(self, inner, log):
        self.inner, self.log = inner, log

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def ladder(self, *a):
        self.log.append("ladder")
        return await self.inner.ladder(*a)

    async def reference_future(self, *a):
        self.log.append("future")
        return await self.inner.reference_future(*a)

    async def option_expiries(self, *a, **k):
        self.log.append("expiries")
        return await self.inner.option_expiries(*a, **k)

    async def calls(self, *a):
        self.log.append("calls")
        return await self.inner.calls(*a)


def _order(log):
    out = []
    for x in log:
        if x == "calls" and out and out[-1] == "calls":
            continue  # many strike batches per chain
        out.append(x)
    return out


def test_future_quoted_before_ladder_from_second_snapshot(tmp_path):
    log: list[str] = []
    go(lambda s: Recorder(s, log), products=[PRODUCTS["forecastex-spx"]], tmp=tmp_path)
    # snapshot 1 (unchanged): ladder, future, expiries, calls..., future
    assert _order(log)[:5] == ["ladder", "future", "expiries", "calls", "future"]
    # snapshot 2: future, ladder, expiries, calls..., future
    assert _order(log)[5:10] == ["future", "ladder", "expiries", "calls", "future"]
    fut = _read(tmp_path, "futures")
    assert (fut["fut_before_ts_ns"] <= fut["fut_ts_ns"]).all()


def test_first_snapshot_keeps_the_ladder_to_future_gate(tmp_path):
    class SlowFut:
        def __init__(self, inner, vc):
            self.inner, self.vc = inner, vc

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def reference_future(self, *a):
            self.vc.t += 6 * 10**9
            return await self.inner.reference_future(*a)

    vc = VClock(ct(11, 30))
    inner = FixtureSource.from_json(SESSION, clock=vc)
    asyncio.run(collect(SlowFut(inner, vc), [PRODUCTS["forecastex-spx"]], D,
                        ParquetSink(tmp_path), vc, vc.sleep))  # fmt: skip
    fut = _read(tmp_path, "futures").sort("ts_ns")
    assert fut["df_flag"][0] is True  # first snapshot: gate applies (ladder before future)
    # later snapshots quote the future before the ladder: the 5 s gate does not apply
    assert fut.filter(pl.col("ts_ns") < FIX - 3600 * 10**9)["df_flag"][1:].any() is False


def test_roll_invalidates_cached_future(tmp_path):
    class Roll:
        """From the third future quote on, the reference future is a different contract."""

        def __init__(self, inner):
            self.inner, self.n = inner, 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def reference_future(self, *a):
            from dataclasses import replace

            f = await self.inner.reference_future(*a)
            self.n += 1
            return replace(f, con_id=f.con_id + 1) if self.n >= 3 else f

    log: list[str] = []
    _, status = go(lambda s: Recorder(Roll(s), log), products=[PRODUCTS["forecastex-spx"]],
                   tmp=tmp_path)  # fmt: skip
    assert status == {"forecastex-spx": "ok"}
    # snapshot 2 (cache hit) sees con_id change on its before-quote: cache dropped, restarted as a
    # first snapshot (ladder before future)
    o = _order(log)
    assert o[5:8] == ["future", "ladder", "future"]  # dropped before-quote, then first-style order
    assert o[8] == "expiries"
    fut = _read(tmp_path, "futures")
    assert fut["snapshot_id"].n_unique() == 97  # every snapshot written exactly once


def test_matched_chain_sigma_preferred_for_df_flag(tmp_path, monkeypatch):
    """chosen = [below (sigma 0.30), match (sigma 0.15)]: the delta must use 0.15."""
    from bmm.spotcheck import collect as C
    from bmm.spotcheck.contracts import ExpiryMatch, OptExpiry
    from bmm.spotcheck.source import SizedOptQuote

    fix = ct(15, 0)
    below = OptExpiry(fix - 600 * 10**9, 1001, "ES", "20261005")
    match = OptExpiry(fix, 1001, "ES", "20261005")
    seen: list[float] = []
    real = C.digital_delta

    class Two:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def option_expiries(self, spec, fut, qd=None):
            return [below, match]

        async def listed_strikes(self, spec, expiry):
            return await self.inner.listed_strikes(spec, match)

        async def calls(self, spec, expiry, strikes):
            out = await self.inner.calls(spec, match, strikes)
            tag = 1 if expiry == below else 2
            return [SizedOptQuote(q.strike, q.call_bid, q.call_ask, tag, tag) for q in out]

    monkeypatch.setattr(
        C, "match_expiry", lambda *a, **k: ExpiryMatch([(below, "below"), (match, "match")], False)
    )
    monkeypatch.setattr(
        C, "atm_sigma", lambda chain, F, T: 0.30 if chain[0].call_bid_size == 1 else 0.15
    )
    monkeypatch.setattr(
        C, "digital_delta", lambda F, K, sigma, T: (seen.append(sigma), real(F, K, sigma, T))[1]
    )
    _, status = go(Two, products=[PRODUCTS["forecastex-spx"]], tmp=tmp_path)
    assert status == {"forecastex-spx": "ok"}
    assert seen and set(seen) == {0.15}


def test_sigma_falls_back_to_first_resolved_when_match_unresolved(tmp_path, monkeypatch):
    from bmm.spotcheck import collect as C
    from bmm.spotcheck.contracts import ExpiryMatch, OptExpiry
    from bmm.spotcheck.source import SizedOptQuote

    fix = ct(15, 0)
    below = OptExpiry(fix - 600 * 10**9, 1001, "ES", "20261005")
    match = OptExpiry(fix, 1001, "ES", "20261005")
    seen: list[float] = []
    real = C.digital_delta

    class Two:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def option_expiries(self, spec, fut, qd=None):
            return [below, match]

        async def listed_strikes(self, spec, expiry):
            return await self.inner.listed_strikes(spec, match)

        async def calls(self, spec, expiry, strikes):
            out = await self.inner.calls(spec, match, strikes)
            tag = 1 if expiry == below else 2
            return [SizedOptQuote(q.strike, q.call_bid, q.call_ask, tag, tag) for q in out]

    monkeypatch.setattr(
        C, "match_expiry", lambda *a, **k: ExpiryMatch([(below, "below"), (match, "match")], False)
    )
    monkeypatch.setattr(
        C, "atm_sigma", lambda chain, F, T: 0.30 if chain[0].call_bid_size == 1 else None
    )
    monkeypatch.setattr(
        C, "digital_delta", lambda F, K, sigma, T: (seen.append(sigma), real(F, K, sigma, T))[1]
    )
    go(Two, products=[PRODUCTS["forecastex-spx"]], tmp=tmp_path)
    assert seen and set(seen) == {0.30}


def test_failed_requote_sets_after_none_and_flags_null(tmp_path):
    class FailSecond:
        """The re-quote (second future call per snapshot) raises."""

        def __init__(self, inner):
            self.inner, self.n = inner, 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def reference_future(self, *a):
            self.n += 1
            # snapshot 1: calls 1 (resolve) and 2 (re-quote); later snapshots: before, after
            if self.n % 2 == 0:
                raise SourceError("no price")
            return await self.inner.reference_future(*a)

    _, status = go(FailSecond, products=[PRODUCTS["forecastex-spx"]], tmp=tmp_path)
    assert status == {"forecastex-spx": "ok"}
    fut = _read(tmp_path, "futures")
    pre = fut.filter(pl.col("ts_ns") < FIX)
    assert pre["und_px_after"].null_count() == pre.height
    assert pre["df_flag"].null_count() == pre.height
    assert (pre["und_px_before"].is_not_null()).all()
