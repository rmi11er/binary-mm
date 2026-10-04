import dataclasses
from datetime import UTC, datetime

import polars as pl

from bmm.scan import store
from bmm.scan.models import Program
from bmm.scan.report import build_report, program_changes

DATE = "2026-10-04"
TS = int(datetime(2026, 10, 4, 12, tzinfo=UTC).timestamp()) * 10**9


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
        "coll_verified": True, "maker_fee": 0.0, "maker_rebate": 0.0, "top_holder_share": None,
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
                    Program("v", m, 12.0, 100.0, 0.05, 5.0, None, "x", False, 0.0, 0.0, "h1")
                ),
            }
        )
        books.append(
            {"ts_ns": TS, "venue": "v", "market_id": m, "bid_prices": [0.49], "bid_sizes": [10.0],
             "bid_holder": [None], "ask_prices": [0.51], "ask_sizes": [10.0], "ask_holder": [None]}
        )  # fmt: skip
    store.write("snapshots", snaps, TS, root)
    store.write("programs", progs, TS, root)
    store.write("books", books, TS, root)


def test_report_has_six_sections(tmp_path):
    seed(tmp_path)
    md = build_report(DATE, tmp_path, adapters={"v": Stub()})
    for h in [
        "## 1. Top 25", "## 2. Venue / category", "## 3. Domain sleeve candidates",
        "## 4. Program changes", "## 5. Exclusions", "## 6. Venue rules",
    ]:  # fmt: skip
        assert h in md


def test_top25_shows_n_per_side_and_allocates(tmp_path):
    seed(tmp_path)
    top = build_report(DATE, tmp_path, adapters={"v": Stub()}).split("## 1.")[1].split("## 2.")[0]
    assert "n_bid" in top and "n_ask" in top and "M1" in top


def test_domain_sleeve_not_dropped_for_missing_reference(tmp_path):
    gold = {"category": "metals", "reference_quality": "model_needed", "market_id": "GOLD-X",
            "markout_30m_side": None, "n_markout_side": 0, "n_trades_side": 0}  # fmt: skip
    seed(tmp_path, extra=[gold])
    sleeve = (
        build_report(DATE, tmp_path, adapters={"v": Stub()}).split("## 3.")[1].split("## 4.")[0]
    )
    assert "GOLD-X" in sleeve


def test_excluded_market_listed_and_not_allocated(tmp_path):
    seed(tmp_path, extra=[{"market_id": "LIVE-1", "excluded": "live in-game"}])
    md = build_report(DATE, tmp_path, adapters={"v": Stub()})
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
