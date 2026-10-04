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


def test_section_2_states_excluded_df_flag_snapshots(report):
    sec = _sections(report[0].read_text())["forecastex-spx"]
    s2 = sec.split("### 2. Mid gap vs fair")[1].split("### 3.")[0]
    assert "0 of 66 pre-fix snapshots excluded" in s2
    assert "df_flag" in s2
