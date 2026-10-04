import re
from datetime import date
from pathlib import Path

import pytest

from bmm.cli import main
from bmm.spotcheck.report import write_report

SESSION = Path(__file__).resolve().parents[2] / "tests/fixtures/spotcheck/session.json"
D = date(2026, 10, 5)
PRODUCTS = ["forecastex-spx", "forecastex-ndx", "forecastex-gold", "cme-gold"]
HEADINGS = [
    "### 1. Ladder quality", "### 2. Mid gap vs fair", "### 3. Taker edge after fees",
    "### 4. Model-free arb edges", "### 5. Persistence", "### 6. Post-settlement window",
    "### 7. Expiry mismatch", "### 8. Verdict",
]  # fmt: skip


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    root = tmp_path_factory.mktemp("sc")
    data = root / "data"
    rc = main(["spotcheck", "collect", "--fixture", str(SESSION), "--question-date", D.isoformat(),
               "--out", str(data)])  # fmt: skip
    assert rc == 0
    out = root / "reports"
    path = write_report(data, D, D, out, name="fixture")
    return path, out


def _sections(md: str) -> dict[str, str]:
    parts = re.split(r"^## (?=\S)", md, flags=re.MULTILINE)[1:]
    return {p.split("\n", 1)[0].strip(): p for p in parts}


def test_headings_in_order(report):
    md = report[0].read_text()
    heads = [ln for ln in md.splitlines() if ln.startswith("### ")]
    assert heads == HEADINGS * 4
    assert "## Summary" in md
    assert report[0].name == "spotcheck_fixture.md"


def test_header_states_fees_and_caveats(report):
    md = report[0].read_text()
    head = md.split("## forecastex-spx")[0]
    assert "0.01, 0.02" in head
    assert "rules03242641956" in head
    assert "owner item #3" in head
    assert "last pre-fix chain" in head


@pytest.mark.parametrize(
    ("product", "label"),
    [("forecastex-spx", "GO"), ("forecastex-ndx", "KILL"), ("forecastex-gold", "INVESTIGATE"),
     ("cme-gold", "INVESTIGATE")],
)  # fmt: skip
def test_fixture_verdicts(report, product, label):
    sec = _sections(report[0].read_text())[product]
    verdict = sec.split("### 8. Verdict")[1]
    assert f"**{label}**" in verdict.split("\n\n")[1]
    summary = report[0].read_text().split("## Summary")[1]
    assert f"- {product}: **{label}**" in summary


def test_pngs_exist(report):
    _, out = report
    for p in PRODUCTS:
        for kind in ("gap_by_moneyness", "persistence"):
            png = out / f"spotcheck_fixture_{p}_{kind}.png"
            assert png.exists() and png.stat().st_size > 1024


def test_both_fee_runs_in_section_3(report):
    sec = _sections(report[0].read_text())["forecastex-spx"]
    s3 = sec.split("### 3. Taker edge after fees")[1].split("### 4.")[0]
    assert "| 0.01 |" in s3 and "| 0.02 |" in s3


def test_mismatch_section_flags_cme_gold(report):
    sec = _sections(report[0].read_text())["cme-gold"]
    s7 = sec.split("### 7. Expiry mismatch")[1].split("### 8.")[0]
    assert "100%" in s7


def test_section_2_has_both_simultaneity_buckets(report):
    for p in ("forecastex-spx", "forecastex-ndx", "forecastex-gold"):
        sec = _sections(report[0].read_text())[p]
        s2 = sec.split("### 2. Mid gap vs fair")[1].split("### 3.")[0]
        assert "**simultaneous**" in s2 and "**non-simultaneous**" in s2
        assert "**Simultaneous bucket**" in s2 and "**Non-simultaneous bucket**" in s2
        assert "gap outside the widened band" in s2 and "median w (c)" in s2
        m = re.search(r"(\d+) simultaneous \((\d+) rows\), (\d+) non-simultaneous", s2)
        assert m and int(m.group(1)) > 0 and int(m.group(3)) > 0, (p, s2[:900])
        s3 = sec.split("### 3. Taker edge after fees")[1].split("### 4.")[0]
        assert "Non-simultaneous bucket" in s3
        s5 = sec.split("### 5. Persistence")[1].split("### 6.")[0]
        assert "Non-simultaneous bucket" in s5


def test_summary_carries_info_verdict_with_nonsimultaneous(report):
    summary = report[0].read_text().split("## Summary")[1]
    for p in PRODUCTS:
        line = next(ln for ln in summary.splitlines() if ln.startswith(f"- {p}:"))
        assert "with non-simultaneous rows included: " in line


def test_section_4_has_dollar_totals_and_scale_note(report):
    md = report[0].read_text()
    for p in PRODUCTS:
        s4 = _sections(md)[p].split("### 4. Model-free arb edges")[1].split("### 5.")[0]
        assert "No venue quote beat" in s4 or ("arb $" in s4 and "payout scale" in s4)


def test_section_2_and_3_state_per_snapshot_and_threshold_columns(report):
    sec = _sections(report[0].read_text())["forecastex-spx"]
    s2 = sec.split("### 2. Mid gap vs fair")[1].split("### 3.")[0]
    assert "% snaps" in s2 and "% obs" in s2
    s3 = sec.split("### 3. Taker edge after fees")[1].split("### 4.")[0]
    assert "edge > 1c" in s3 and "edge > 2c" in s3 and "payout scale" in s3


def test_section_7_has_tilt_by_role(report):
    s7 = _sections(report[0].read_text())["cme-gold"].split("### 7. Expiry mismatch")[1]
    assert "Tilt by chain role" in s7 and "above" in s7.split("Tilt by chain role")[1]


def test_section_2_has_share_by_ttf_and_tick_floor_check(report):
    s2 = (
        _sections(report[0].read_text())["forecastex-spx"]
        .split("### 2. Mid gap vs fair")[1]
        .split("### 3.")[0]
    )
    assert "Simultaneous share of pre-fix snapshots by time to fix" in s2
    assert ">2 (all rows)" in s2 and ">2 (fair in 0.01..0.99)" in s2
    assert "owner decides whether the rule should exclude tick-floor rows" in s2
    assert "missing futures row" in s2 and "counts as non-simultaneous" in s2


def test_wording_q_m3_m6_m7(report):
    md = report[0].read_text()
    s4 = _sections(md)["forecastex-spx"].split("### 4. Model-free arb edges")[1].split("### 5.")[0]
    assert "No venue quote beat" in s4 or "not replication-grade when w > 0" in s4
    s7 = _sections(md)["cme-gold"].split("### 7. Expiry mismatch")[1]
    assert "tilt > 0" in s7 and "tilt < 0" in s7
    head = md.split("## forecastex-spx")[0]
    assert (
        "deliberately ignored" in head
        and "Non-simultaneity threshold: |dF| x digital delta > 1c" in head
    )


def test_section_6_has_outcome_column(report):
    s6 = (
        _sections(report[0].read_text())["forecastex-spx"]
        .split("### 6. Post-settlement window")[1]
        .split("### 7.")[0]
    )
    assert "settled outcome" in s6 and "[?]" in s6


def test_nonsim_threshold_flag_changes_the_buckets(tmp_path):
    data = tmp_path / "data"
    assert main(["spotcheck", "collect", "--fixture", str(SESSION), "--question-date", D.isoformat(),
                 "--out", str(data)]) == 0  # fmt: skip
    outs = {}
    for thr in ("0.01", "0.5"):
        rc = main(
            [
                "spotcheck",
                "report",
                "--from",
                "2026-10-05",
                "--to",
                "2026-10-05",
                "--data",
                str(data),
                "--out",
                str(tmp_path / thr),
                "--name",
                "t",
                "--nonsim-threshold",
                thr,
            ]
        )
        assert rc == 0
        outs[thr] = (tmp_path / thr / "spotcheck_t.md").read_text()
    assert "greater than" not in outs["0.5"]
    m1 = re.search(
        r"Pre-fix snapshots: (\d+) simultaneous .*?, (\d+) non-simultaneous", outs["0.01"]
    )
    m2 = re.search(
        r"Pre-fix snapshots: (\d+) simultaneous .*?, (\d+) non-simultaneous", outs["0.5"]
    )
    assert int(m1.group(2)) > 0 and int(m2.group(2)) == 0  # 50c threshold: nothing is flagged
