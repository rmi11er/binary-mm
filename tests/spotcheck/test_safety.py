import ast
import pathlib
import re

import pytest

from bmm.spotcheck.safety import ALLOWED_PORTS, SafetyError, check_mode, check_port

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPOT = ROOT / "src/bmm/spotcheck"
FORBIDDEN = re.compile(r"placeOrder|cancelOrder|reqGlobalCancel|whatIfOrder|bracketOrder|Order\(")


@pytest.mark.parametrize("port", [4001, 7496, 7497, 0, 4003])
def test_refuses_non_paper_ports(port):
    with pytest.raises(SafetyError):
        check_port(port)


def test_allows_only_4002():
    assert ALLOWED_PORTS == frozenset({4002}) and check_port(4002) == 4002


def test_refuses_live_mode():
    with pytest.raises(SafetyError):
        check_mode({"BMM_MODE": "live"})
    check_mode({"BMM_MODE": "record"})
    check_mode({})


def _files():
    files = sorted(SPOT.rglob("*.py"))
    cli = ROOT / "src/bmm/cli.py"
    return files + ([cli] if cli.exists() else [])


def test_no_order_calls_in_spotcheck():
    hits = [
        f"{p}:{i}"
        for p in _files()
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if FORBIDDEN.search(line)
    ]
    assert hits == []


def test_no_order_imports_from_ib_async():
    bad = []
    for p in _files():
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("ib_async"):
                bad += [
                    f"{p}:{a.name}" for a in node.names if a.name.endswith("Order") or a.name == "*"
                ]
    assert bad == []


def test_guard_test_catches_a_violation(tmp_path):
    assert FORBIDDEN.search("ib.placeOrder(c, LimitOrder('BUY', 1, 0.5))")
