import dataclasses

import pytest

from bmm.scan import store
from bmm.scan.models import Program
from bmm.scan.report import analyze, build_report, sensitivity
from bmm.scan.schedule import make_schedule

from .test_report import ADS, DATE, GEM, TS, Stub, snap

H = 3600 * 10**9


class BadStub(Stub):
    """A defective adapter: pays 3x the pool rate."""

    def expected_reward(self, prog, s, side, book):
        return 3 * prog.R_usd_hr * s / (s + 10) if s > 0 else 0.0


BASE = {
    "venue": "v",
    "R_usd_hr": 6.0,
    "target_size": 100.0,
    "max_spread": 0.05,
    "venue_min": 5.0,
    "discount": None,
    "scoring": "x",
    "two_sided": False,
    "maker_fee": 0.0,
    "maker_rebate": 0.0,
    "raw_hash": "h",
}


def seed2(root, markets):
    snaps, progs, books = [], [], []
    for m, sk, pk in markets:
        snaps += [snap(m, "bid", **sk), snap(m, "ask", **sk)]
        progs.append({"ts_ns": TS, **dataclasses.asdict(Program(**(BASE | {"market_id": m} | pk)))})
        books.append(
            {"ts_ns": TS, "venue": "v", "market_id": m, "bid_prices": [0.49], "bid_sizes": [10.0],
             "bid_holder": [None], "ask_prices": [0.51], "ask_sizes": [10.0], "ask_holder": [None]}
        )  # fmt: skip
    store.write("snapshots", snaps, TS, root)
    store.write("programs", progs, TS, root)
    store.write("books", books, TS, root)


def test_roc_over_100pct_per_day_is_defect_excluded_and_listed(tmp_path):
    seed2(tmp_path, [("OK1", {}, {}), ("HOT", {}, {"R_usd_hr": 50.0})])
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert {d["market"] for d in a.defects} == {"v:HOT"}
    assert any("RoC" in r for d in a.defects for r in d["reasons"])
    assert not any("HOT" in k and x.s > 0 for k, x in a.allocs.items())
    assert any("OK1" in k and x.s > 0 for k, x in a.allocs.items())
    md = build_report(DATE, tmp_path, adapters=ADS, conf=GEM)
    sec = md.split("## 7. DEFECT")[1].split("## 8.")[0]
    assert (
        "DEFECT half-markets: 2" in sec
        and "HOT" in sec
        and "R_side" in sec
        and "pool_remaining" in sec
    )
    assert "HOT" not in md.split("## 1.")[1].split("## 2.")[0]


def test_reward_above_pool_remaining_is_defect(tmp_path):
    seed2(tmp_path, [("BAD", {}, {})])
    a = analyze(DATE, tmp_path, {"v": BadStub()}, GEM)
    assert a.defects and any("pool" in r for d in a.defects for r in d["reasons"])
    assert not any(x.s > 0 for x in a.allocs.values())


def test_short_program_scales_reward_by_actual_schedule(tmp_path):
    short = {"schedule": make_schedule([(None, TS + H, 6.0)]), "end_ns": TS + H, "pool_usd": 6.0}
    seed2(tmp_path, [("LONG", {}, {}), ("SHORT", {}, short)])
    a = analyze(DATE, tmp_path, ADS, GEM)
    lo, sh = a.allocs["v:LONG:bid"], a.allocs["v:SHORT:bid"]
    assert lo.s == sh.s > 0
    assert sh.reward_day == pytest.approx(lo.reward_day / 24)  # 1h of the program, not rate x 24
    assert not a.defects


def test_no_scheduled_reward_in_next_24h_is_listed_not_allocated(tmp_path):
    later = {"schedule": make_schedule([(TS + 30 * H, TS + 40 * H, 6.0)])}
    seed2(tmp_path, [("FUT", {}, later), ("OK1", {}, {})])
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert not any("FUT" in k and x.s > 0 for k, x in a.allocs.items())
    assert any(m == "FUT" and "24h" in why for _, m, why in a.blocked)


def test_shares_assumption_scales_proxied_fills(tmp_path):
    shares = {"fills_shares_hr": 20.0, "fills_proxied": True, "fills_side_hr": 10.0}
    seed2(tmp_path, [("PM1", shares, {}), ("T1", {}, {})])
    a1 = analyze(DATE, tmp_path, ADS, GEM, shares_factor=1.0)
    a2 = analyze(DATE, tmp_path, ADS, GEM, shares_factor=0.5)
    assert a1.fills["v:PM1:bid"] == pytest.approx(10.0) and a2.fills["v:PM1:bid"] == pytest.approx(
        5.0
    )
    assert (
        a1.fills["v:T1:bid"] == a2.fills["v:T1:bid"] == pytest.approx(1.0)
    )  # tape venues unaffected


def test_sensitivity_reports_whether_top10_changes(tmp_path):
    seed2(tmp_path, [("A", {}, {}), ("B", {}, {})])
    s = sensitivity(DATE, tmp_path, ADS, GEM, n=10)
    assert s["changed"] is False and s["top_a"] == s["top_b"]  # no shares-based fills -> identical
    shares = {"fills_shares_hr": 4000.0, "fills_proxied": True}
    seed2(
        tmp_path / "x",
        [
            ("P1", shares, {}),
            ("P2", {"fills_shares_hr": 2000.0, "fills_proxied": True}, {}),
            ("T", {}, {}),
        ],
    )
    s2 = sensitivity(DATE, tmp_path / "x", ADS, GEM, n=10)
    assert s2["changed"] == (s2["top_a"] != s2["top_b"]) and set(s2) >= {
        "top_a",
        "top_b",
        "changed",
        "moved",
    }


def test_intermittent_program_judged_per_active_hour(tmp_path):
    hot = {
        "schedule": make_schedule([(None, TS + H, 15.0)]),
        "end_ns": TS + H,
        "pool_usd": 15.0,
        "R_usd_hr": 15.0,
    }
    seed2(tmp_path, [("BURST", {}, hot)])
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert a.defects and all(
        "per active hour" in r for d in a.defects for r in d["reasons"] if "RoC" in r
    )


def test_zero_collateral_is_a_defect(tmp_path):
    seed2(tmp_path, [("FREE", {"coll_side": 0.0}, {}), ("OK1", {}, {})])
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert any(
        "collateral" in r for d in a.defects for r in d["reasons"] if d["market"] == "v:FREE"
    )
    assert not any("FREE" in k and x.s > 0 for k, x in a.allocs.items())


def test_tiny_posted_collateral_is_flagged_on_the_collateral_basis(tmp_path):
    seed2(
        tmp_path, [("TAIL", {"coll_side": 0.005}, {}), ("OK1", {}, {})]
    )  # RoC is on COLLATERAL, per spec
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert any(d["market"] == "v:TAIL" and any("RoC" in r for r in d["reasons"]) for d in a.defects)
    assert not any(d["market"] == "v:OK1" for d in a.defects)


def test_budget_limited_small_size_is_listed_not_excluded(tmp_path):
    seed2(
        tmp_path, [(f"M{i}", {}, {}) for i in range(20)]
    )  # 20 markets share the 10% stress budget
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert a.defects == []  # at the standalone size RoC is well under 100%/day
    assert a.over_at_alloc and all(o["roc"] > 1.0 for o in a.over_at_alloc)
    assert sum(1 for x in a.allocs.values() if x.s > 0) > 0  # still allocated
    assert "Allocated above 100%/day" in build_report(DATE, tmp_path, adapters=ADS, conf=GEM)


MINUTE = 60 * 10**9


def stream_pk(rate=80.0, windows=100, window_min=60):
    w = window_min * MINUTE
    segs = [(TS + i * w, TS + (i + 1) * w, rate) for i in range(windows)]
    return {"R_usd_hr": rate, "schedule": make_schedule(segs), "stream_windows": windows,
            "start_ns": TS, "end_ns": TS + w, "pool_usd": rate * window_min / 60}  # fmt: skip


def test_high_return_recurring_stream_is_needs_review_not_defect(tmp_path):
    seed2(tmp_path, [("STREAM", {}, stream_pk()), ("PLAIN", {}, {"R_usd_hr": 80.0})])
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert {d["market"] for d in a.defects} == {"v:PLAIN"}  # same return, not a stream -> DEFECT
    assert {d["market"] for d in a.review} == {"v:STREAM"}  # recurring windows -> review class
    assert all(any("recurring" in r for r in d["reasons"]) for d in a.review)
    assert not any("STREAM" in k and x.s > 0 for k, x in a.allocs.items())  # held out of allocation
    md = build_report(DATE, tmp_path, adapters=ADS, conf=GEM)
    assert "needs_review_recurring" in md and "## 7b." in md
    assert "DEFECT half-markets: 2" in md  # PLAIN's two halves only; review items are not counted


def test_sane_stream_is_allocated_and_reuses_collateral_across_windows(tmp_path):
    seed2(tmp_path, [("SANE", {}, stream_pk(rate=6.0)), ("ONE", {}, {})])
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert not a.review and not a.defects
    s, o = a.allocs["v:SANE:bid"], a.allocs["v:ONE:bid"]
    assert s.s == o.s > 0 and s.collateral == pytest.approx(
        o.collateral
    )  # per-window requirement, reused
    assert s.reward_day == pytest.approx(o.reward_day)  # 24 effective hours either way


def test_pool_breach_on_a_stream_is_still_a_defect(tmp_path):
    seed2(tmp_path, [("STREAM", {}, stream_pk(rate=6.0))])
    a = analyze(DATE, tmp_path, {"v": BadStub()}, GEM)
    assert a.defects and not a.review


def test_metals_section_lists_15m_streams_as_latency_game_and_other_metals_programs(tmp_path):
    seed2(tmp_path, [
        ("KXPALLADIUM15M-X", {"category": "metals"}, stream_pk(window_min=15)),
        ("KXSILVERD-Y", {"category": "metals"}, {"R_usd_hr": 1.0, "start_ns": TS, "end_ns": TS + 24 * 60 * MINUTE}),
        ("KXUSDJPY15M-Z", {"category": "fx"}, stream_pk(window_min=15)),
    ])  # fmt: skip
    md = build_report(DATE, tmp_path, adapters=ADS, conf=GEM)
    sec = md.split("## 9. Metals 15-minute streams")[1].split("## 9b.")[0]
    assert "KXPALLADIUM15M-X" in sec and "latency_game_excluded" in sec
    assert "KXUSDJPY15M-Z" not in sec  # not metals
    assert "Metals programs with a period > 1h this scan: 1" in sec and "KXSILVERD-Y" in sec
    lat = md.split("## 9b. latency-game, excluded")[1].split("## 10.")[0]
    assert (
        "KXPALLADIUM15M-X" in lat and "KXUSDJPY15M-Z" in lat
    )  # every 15-minute series, any category
    assert "KXSILVERD-Y" not in lat


def test_15m_series_are_not_allocated_by_default_but_are_when_included(tmp_path):
    seed2(
        tmp_path,
        [
            ("KXGOLD15M-A", {"category": "metals"}, stream_pk(rate=6.0, window_min=15)),
            ("OK1", {}, {}),
        ],
    )
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert not any("KXGOLD15M-A" in k and x.s > 0 for k, x in a.allocs.items())
    assert {a.status[k] for k in a.status if "KXGOLD15M-A" in k} == {"latency_game_excluded"}
    assert not a.defects and not a.review  # excluded before the guard: not DEFECT, not review
    b = analyze(DATE, tmp_path, ADS, GEM, include_latency=True)
    assert any(
        "KXGOLD15M-A" in k and x.s > 0 for k, x in b.allocs.items()
    )  # --include-latency-game


def test_latency_exclusion_follows_the_period_even_without_a_15m_ticker(tmp_path):
    seed2(
        tmp_path, [("FASTMKT", {}, {"start_ns": TS, "end_ns": TS + 15 * MINUTE, "R_usd_hr": 6.0})]
    )
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert {a.status[k] for k in a.status} == {"latency_game_excluded"}


class Stub2(Stub):
    """Deep competing book: our share of the pool stays small."""

    def expected_reward(self, prog, s, side, book):
        return 0.5 * prog.R_usd_hr * s / (s + 10000) if s > 0 else 0.0


def test_needs_manual_check_flags_reward_above_half_the_pool_only(tmp_path):
    seed2(tmp_path, [("TOP", {}, {})])  # Stub: we take ~97% of the pool
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert any(m["market"] == "v:TOP" and any("50%" in r for r in m["reasons"]) for m in a.manual)
    assert all("needs_manual_check" in a.flags[k] for k in a.flags)
    seed2(tmp_path / "deep", [("DEEP", {}, {})])
    b = analyze(DATE, tmp_path / "deep", {"v": Stub2()}, GEM)
    assert not any(m["market"] == "v:DEEP" for m in b.manual)  # small share: no flag


def test_alerts_section_lists_programs_on_watched_series(tmp_path):
    seed2(tmp_path, [("OK1", {}, {})])
    row = {"venue": "kalshi", "series": "KXGOLDH", "ticker": "KXGOLDH-26OCT0610-T3000", "id": "i1", "status": "upcoming",
           "pool_usd": 20.0, "start_ns": TS, "end_ns": TS + 60 * MINUTE, "target": 300.0, "ts_ns": TS, "new": True}  # fmt: skip
    store.write("alerts", [row], TS, tmp_path)
    md = build_report(DATE, tmp_path, adapters=ADS, conf=GEM)
    sec = md.split("## 11. Alerts")[1]
    assert "KXGOLDH-26OCT0610-T3000" in sec and "NEW" in sec
    empty = build_report(DATE, tmp_path / "none", adapters=ADS, conf=GEM)
    assert "No snapshots" in empty
