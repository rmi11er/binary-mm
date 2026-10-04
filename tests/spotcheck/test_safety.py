import ast
import pathlib
import re
import subprocess
import sys
import textwrap

import numpy as np
import pytest

from bmm.spotcheck.safety import (
    ALLOWED_PORTS,
    SafetyError,
    check_accounts,
    check_host,
    check_mode,
    check_port,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPOT = ROOT / "src/bmm/spotcheck"
FORBIDDEN = re.compile(
    r"placeOrder|cancelOrder|reqGlobalCancel|whatIfOrder|bracketOrder|oneCancelsAll"
    r"|exerciseOptions|Order\s*\("
)
ORDER_ATTRS = {
    "placeOrder", "cancelOrder", "reqGlobalCancel", "whatIfOrder", "whatIfOrderAsync",
    "bracketOrder", "oneCancelsAll", "exerciseOptions", "reqAutoOpenOrders", "reqOpenOrders",
}  # fmt: skip
BAD_MODULES = (
    "bmm.gateway", "bmm.quoter", "bmm.hedger", "bmm.risk", "bmm.pricing.quoting", "ib_async.order",
)  # fmt: skip


@pytest.mark.parametrize("port", [4001, 7496, 7497, 0, 4003, 4002.0, np.int64(4002), True])
def test_refuses_non_paper_ports(port):
    with pytest.raises(SafetyError):
        check_port(port)


def test_allows_only_4002():
    assert ALLOWED_PORTS == frozenset({4002}) and check_port(4002) == 4002


@pytest.mark.parametrize(
    "mode", ["live", "LIVE", " live", "live\n", "Live ", "livе", "prod", "paper2", "x"]
)
def test_refuses_live_and_unknown_modes(mode):
    with pytest.raises(SafetyError):
        check_mode({"BMM_MODE": mode})


@pytest.mark.parametrize("mode", ["record", "paper", "sim", " Record ", ""])
def test_allows_known_modes(mode):
    check_mode({"BMM_MODE": mode})
    check_mode({})


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_allowed(host):
    assert check_host(host) == host


@pytest.mark.parametrize("host", ["10.0.0.5", "example.com", "127.0.0.1.evil.com", ""])
def test_remote_hosts_refused_unless_allowlisted(host):
    with pytest.raises(SafetyError):
        check_host(host)
    if host:
        assert check_host(host, allow=[host]) == host


def test_accounts_must_be_paper():
    assert check_accounts(["DU123", "DU456"]) == ["DU123", "DU456"]
    for bad in ([], ["U123"], ["DU1", "U2"], ["du1"]):
        with pytest.raises(SafetyError):
            check_accounts(bad)


def scan(paths):
    """Return 'path:reason' for every order-surface hit in the given files."""
    hits = []
    for p in paths:
        text = p.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if FORBIDDEN.search(line):
                hits.append(f"{p.name}:{i}:regex")
        for node in ast.walk(ast.parse(text)):
            where = f"{p.name}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.startswith("ib_async"):
                    if mod.startswith("ib_async.order"):
                        hits.append(f"{where}:ib_async.order")
                    hits += [
                        f"{where}:{a.name}"
                        for a in node.names
                        if a.name.endswith("Order") or a.name == "*"
                    ]
                if mod == "bmm.pricing" or any(
                    mod == m or mod.startswith(m + ".") for m in BAD_MODULES
                ):
                    hits.append(f"{where}:import {mod}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "importlib" or any(
                        a.name == m or a.name.startswith(m + ".") for m in BAD_MODULES
                    ):
                        hits.append(f"{where}:import {a.name}")
            elif isinstance(node, ast.Attribute) and node.attr in ORDER_ATTRS:
                hits.append(f"{where}:attr {node.attr}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"__import__", "import_module"}:
                    hits.append(f"{where}:dynamic import")
                if node.func.id in {"getattr", "setattr", "eval", "exec"}:
                    lit = len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                    if node.func.id != "getattr" or not lit or "order" in str(
                        node.args[1].value
                    ).lower():
                        hits.append(f"{where}:{node.func.id}")
    return hits


def _files():
    files = sorted(SPOT.rglob("*.py"))
    cli = ROOT / "src/bmm/cli.py"
    return files + ([cli] if cli.exists() else [])


def test_file_walk_is_not_vacuous():
    names = {p.name for p in _files()}
    assert "safety.py" in names and "analyze.py" in names
    assert len(names) >= 7


def test_no_order_surface_in_spotcheck():
    assert scan(_files()) == []


EVASIONS = {
    "getattr_concat": 'x = getattr(ib, "place" + "Order")',
    "getattr_dynamic": "x = getattr(ib, name)",
    "limit_space": "o = LimitOrder (1)",
    "alias_import": "from ib_async import LimitOrder as LO",
    "star_import": "from ib_async import *",
    "order_module": "from ib_async.order import MarketOrder",
    "importlib": "import importlib",
    "dunder_import": '__import__("ib_async")',
    "exercise": "ib.exerciseOptions(c, 1, 1, 'a', 'b')",
    "oca": "ib.oneCancelsAll(o, 'x', 1)",
    "gateway": "from bmm.gateway import x",
    "quoting": "from bmm.pricing.quoting import to_forecastex_orders",
    "pricing_pkg": "from bmm.pricing import black76_call",
    "attr_whatif": "ib.client.whatIfOrderAsync",
}


@pytest.mark.parametrize("name", sorted(EVASIONS))
def test_scan_catches_evasion(tmp_path, name):
    f = tmp_path / f"{name}.py"
    f.write_text(textwrap.dedent(EVASIONS[name]) + "\n")
    assert scan([f]), name


def test_entry_points_do_not_pull_order_modules():
    code = (
        "import pkgutil, importlib, sys, bmm.spotcheck as s\n"
        "for m in pkgutil.iter_modules(s.__path__):\n"
        "    importlib.import_module('bmm.spotcheck.' + m.name)\n"
        "import bmm.cli\n"
        "bad = [m for m in sys.modules if m.startswith(('bmm.gateway','bmm.quoter',"
        "'bmm.hedger','bmm.risk','bmm.pricing.quoting'))]\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=False,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""},
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
