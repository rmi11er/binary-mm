import dataclasses
from datetime import UTC, datetime

import polars as pl
import pytest

from bmm.scan import store
from bmm.scan.config import ScanConfig, VenueCfg
from bmm.scan.models import Program
from bmm.scan.report import analyze, build_report, program_changes, refresh_ranking, top_candidates

DATE = "2026-10-04"
TS = int(datetime(2026, 10, 4, 12, tzinfo=UTC).timestamp()) * 10**9


ADS = {}


class Stub:
    """Scoring-only adapter: pro-rata reward on a fixed competing depth of 10."""

    name = "v"

    def expected_reward(self, prog, s, side, book):
        return 0.5 * prog.R_usd_hr * s / (s + 10) if s > 0 else 0.0

    def qualifying_min(self, prog, side, book):
        return 5

    def s_star_closed_form(self, prog, side, book, c):
        return None


def snap(market, side, **kw):
    d = {
        "ts_ns": TS, "venue": "v", "market_id": market, "side": side, "title": market,
        "category": "sports", "D_side": 10.0, "depth_raw_side": 50.0, "fills_side_hr": 1.0,
        "markout_30m_side": -0.01, "n_trades_side": 25, "n_markout_side": 25, "coll_side": 0.5,
        "coll_verified": True, "bid": 0.49, "ask": 0.51, "maker_fee": 0.0, "maker_rebate": 0.0, "top_holder_share": None,
        "reference_quality": "none", "excluded": None,
    }  # fmt: skip
    return d | kw


def seed(root, extra=()):
    markets = [("M1", {}), ("M2", {"category": "politics"})] + [(e["market_id"], e) for e in extra]
    snaps, progs, books = [], [], []
    for m, kw in markets:
        kw = {k: v for k, v in kw.items() if k != "market_id"}
        snaps += [snap(m, "bid", **kw), snap(m, "ask", **kw)]
        progs.append(
            {
                "ts_ns": TS,
                **dataclasses.asdict(
                    Program(
                        kw.get("venue", "v"),
                        m,
                        6.0,
                        100.0,
                        0.05,
                        5.0,
                        None,
                        "x",
                        False,
                        0.0,
                        0.0,
                        "h1",
                    )
                ),
            }
        )
        books.append(
            {"ts_ns": TS, "venue": kw.get("venue", "v"), "market_id": m, "bid_prices": [0.49], "bid_sizes": [10.0],
             "bid_holder": [None], "ask_prices": [0.51], "ask_sizes": [10.0], "ask_holder": [None]}
        )  # fmt: skip
    store.write("snapshots", snaps, TS, root)
    store.write("programs", progs, TS, root)
    store.write("books", books, TS, root)


def test_report_has_six_sections(tmp_path):
    seed(tmp_path)
    md = build_report(DATE, tmp_path, adapters=ADS)
    for h in [
        "## 1. Top 25", "## 2. Venue / category", "## 3. Domain sleeve candidates",
        "## 4. Program changes", "## 5. Exclusions", "## 6. Venue rules",
    ]:  # fmt: skip
        assert h in md


def test_top25_shows_n_per_side_and_allocates(tmp_path):
    seed(tmp_path)
    top = build_report(DATE, tmp_path, adapters=ADS).split("## 1.")[1].split("## 2.")[0]
    assert "n_bid" in top and "n_ask" in top and "M1" in top


def test_domain_sleeve_not_dropped_for_missing_reference(tmp_path):
    gold = {"category": "metals", "reference_quality": "model_needed", "market_id": "GOLD-X",
            "markout_30m_side": None, "n_markout_side": 0, "n_trades_side": 0}  # fmt: skip
    seed(tmp_path, extra=[gold])
    sleeve = build_report(DATE, tmp_path, adapters=ADS).split("## 3.")[1].split("## 4.")[0]
    assert "GOLD-X" in sleeve


def test_excluded_market_listed_and_not_allocated(tmp_path):
    seed(tmp_path, extra=[{"market_id": "LIVE-1", "excluded": "live in-game"}])
    md = build_report(DATE, tmp_path, adapters=ADS)
    assert "LIVE-1" in md.split("## 5.")[1].split("## 6.")[0]
    assert "LIVE-1" not in md.split("## 1.")[1].split("## 2.")[0]


def test_first_scan_program_diff():
    cur = pl.DataFrame({"venue": ["pm_us"], "market_id": ["a"], "raw_hash": ["h"]})
    assert program_changes(cur, None)["change"].to_list() == ["new"]


def test_program_changed_and_ended():
    prev = pl.DataFrame({"venue": ["v", "v"], "market_id": ["a", "b"], "raw_hash": ["h1", "h"]})
    cur = pl.DataFrame({"venue": ["v"], "market_id": ["a"], "raw_hash": ["h2"]})
    assert sorted(program_changes(cur, prev)["change"].to_list()) == ["changed", "ended"]


def test_empty_date_does_not_crash(tmp_path):
    assert "No snapshots" in build_report(DATE, tmp_path, adapters={})


ADS.update({"v": Stub(), "gemini": Stub(), "kalshi": Stub()})
GEM = ScanConfig(
    frozenset(), {"gemini": VenueCfg("gemini", {"enabled": True, "pool_scope_verified": False})}, {}
)


def test_gemini_scanned_and_reported_but_not_allocated(tmp_path):
    seed(tmp_path, extra=[{"market_id": "G1", "venue": "gemini"}])
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert not any("G1" in k and x.s > 0 for k, x in a.allocs.items())
    assert any(x.s > 0 for k, x in a.allocs.items() if "M1" in k)
    md = build_report(DATE, tmp_path, adapters=ADS, conf=GEM)
    assert "G1" in md.split("## 5.")[1].split("## 6.")[0] and "pool_scope_verified" in md
    assert "G1" not in md.split("## 1.")[1].split("## 2.")[0]


def test_stress_caps_hold_and_are_reported(tmp_path):
    hi = {"jump_class": "high", "market_id": "POL-1"}
    seed(tmp_path, extra=[hi])
    a = analyze(DATE, tmp_path, ADS, GEM)
    pol = [x for k, x in a.allocs.items() if "POL-1" in k]
    assert pol and sum(x.stress_loss for x in pol) <= 160 + 1e-6
    assert sum(x.stress_loss for x in a.allocs.values()) <= 800 + 1e-6
    assert all(x.stress_loss == pytest.approx(x.s * 0.49) for x in pol)  # high: distance to 0 / 1
    top = build_report(DATE, tmp_path, adapters=ADS, conf=GEM).split("## 1.")[1].split("## 2.")[0]
    assert "stress" in top and "POL-1" in top


def test_proxied_markout_blends_toward_organic(tmp_path):
    prox = {"market_id": "P1", "markout_30m_side": None, "n_markout_side": 0,
            "markout_proxied_side": -0.02, "n_markout_proxied_side": 30, "fills_proxied": True}  # fmt: skip
    org = {"market_id": "P2", "markout_30m_side": -0.04, "n_markout_side": 25,
           "markout_proxied_side": -0.02, "n_markout_proxied_side": 30}  # fmt: skip
    seed(tmp_path, extra=[prox, org])
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert a.markout["v:P1:bid"] == (pytest.approx(-0.02), "proxied")
    assert a.markout["v:P2:bid"] == (pytest.approx(-0.04), "organic")  # organic (n>=20) replaces
    top = build_report(DATE, tmp_path, adapters=ADS, conf=GEM).split("## 1.")[1].split("## 2.")[0]
    p1 = next(ln for ln in top.splitlines() if "P1" in ln)
    p2 = next(ln for ln in top.splitlines() if "P2" in ln)
    assert "proxied" in p1 and "proxied" not in p2


def test_vol_proxy_uses_kalshi_calibrated_lambda(tmp_path):
    k = {"market_id": "K1", "venue": "kalshi", "markout_30m_side": None, "n_markout_side": 0,
         "markout_proxied_side": -0.02, "n_markout_proxied_side": 40, "vol30_proxy": 0.04}  # fmt: skip
    pm = {"market_id": "PM1", "markout_30m_side": None, "n_markout_side": 0, "vol30_proxy": 0.06,
          "fills_proxied": True}  # fmt: skip
    seed(tmp_path, extra=[k, pm])
    a = analyze(DATE, tmp_path, ADS, GEM)
    assert a.lam == pytest.approx(0.5)  # 40*0.02 / (40*0.04)
    assert a.markout["v:PM1:bid"] == (pytest.approx(-0.03), "vol-proxy")  # -lambda * 0.06
    assert {"mk:proxied", "fills:proxied"} <= set(
        a.flags["v:PM1:bid"]
    )  # every derived input is flagged


def test_ranking_table_written_for_fast_tier(tmp_path):
    seed(tmp_path)
    refresh_ranking(DATE, tmp_path, ADS, GEM)
    cand = top_candidates(tmp_path, 40, DATE)
    assert ("v", "M1") in cand and len(cand) <= 40
