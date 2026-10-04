import sys
from pathlib import Path

import polars as pl
import pytest

from bmm.cli import main

ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / "tests/fixtures/spotcheck/session.json"
IBS = "bmm.spotcheck.ibsource"


def test_bad_port_refused_without_loading_ibsource(monkeypatch, capsys):
    monkeypatch.delitem(sys.modules, IBS, raising=False)
    monkeypatch.delenv("BMM_MODE", raising=False)
    assert main(["spotcheck", "collect", "--port", "4001", "--product", "forecastex-spx"]) == 3
    assert IBS not in sys.modules
    assert "REFUSED" in capsys.readouterr().err


def test_live_mode_refused(monkeypatch):
    monkeypatch.delitem(sys.modules, IBS, raising=False)
    monkeypatch.setenv("BMM_MODE", "live")
    assert main(["spotcheck", "collect", "--product", "forecastex-spx"]) == 3
    assert IBS not in sys.modules


def test_remote_host_refused_unless_allowlisted(monkeypatch):
    monkeypatch.delitem(sys.modules, IBS, raising=False)
    monkeypatch.delenv("BMM_MODE", raising=False)
    assert main(["spotcheck", "collect", "--host", "10.0.0.9"]) == 3
    assert IBS not in sys.modules


def test_fixture_run_writes_three_tables(tmp_path, monkeypatch):
    monkeypatch.delenv("BMM_MODE", raising=False)
    rc = main(["spotcheck", "collect", "--fixture", str(SESSION), "--question-date", "2026-10-05",
               "--out", str(tmp_path)])  # fmt: skip
    assert rc == 0
    day = tmp_path / "date=2026-10-05"
    for t in ("ladder", "futures", "options"):
        assert pl.read_parquet(day / f"{t}.parquet").height > 0


def test_fixture_run_without_question_date_uses_session_date(tmp_path, monkeypatch):
    monkeypatch.delenv("BMM_MODE", raising=False)
    assert main(["spotcheck", "collect", "--fixture", str(SESSION), "--product",
                 "forecastex-gold", "--out", str(tmp_path)]) == 0  # fmt: skip
    assert (tmp_path / "date=2026-10-05/ladder.parquet").exists()


def test_all_products_failing_exits_2(tmp_path, monkeypatch):
    monkeypatch.delenv("BMM_MODE", raising=False)
    empty = tmp_path / "empty.json"
    empty.write_text('{"question_date": "2026-10-05", "seed": 7, "products": {}}')
    assert main(["spotcheck", "collect", "--fixture", str(empty), "--product", "forecastex-spx",
                 "--out", str(tmp_path / "o")]) == 2  # fmt: skip


def test_unknown_product_exits_2(tmp_path, monkeypatch):
    monkeypatch.delenv("BMM_MODE", raising=False)
    assert main(["spotcheck", "collect", "--fixture", str(SESSION), "--product", "nope",
                 "--out", str(tmp_path)]) == 2  # fmt: skip


def test_live_flag_does_not_exist():
    with pytest.raises(SystemExit) as e:
        main(["spotcheck", "collect", "--live"])
    assert e.value.code == 2


def test_report_stub_exits_2(capsys):
    assert main(["spotcheck", "report", "--from", "2026-10-05", "--to", "2026-10-05"]) == 2
    assert "not implemented" in capsys.readouterr().err


def test_dispatcher_usage(capsys):
    assert main([]) == 2
