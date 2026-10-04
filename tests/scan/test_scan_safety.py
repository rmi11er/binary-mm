import asyncio
from pathlib import Path

import pytest

from bmm.cli import main
from bmm.scan.http import GetOnlyClient

SCAN = Path(__file__).resolve().parents[2] / "src/bmm/scan"


@pytest.mark.parametrize("case", ["live_env", "ib_import", "foreign_host"])
def test_scan_port_env(case, monkeypatch):
    if case == "live_env":
        monkeypatch.setenv("BMM_MODE", "live")
        assert main(["scan", "rewards", "--once"]) != 0
    elif case == "ib_import":
        src = "\n".join(p.read_text() for p in SCAN.rglob("*.py"))
        assert "ib_async" not in src and "4001" not in src
    else:
        with pytest.raises(ValueError):
            asyncio.run(GetOnlyClient({"api.example.com"}).get_json("https://evil.example.org/x"))
