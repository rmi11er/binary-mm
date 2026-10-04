import ast
import pathlib
import re
import subprocess
import sys
import textwrap

import pytest

from bmm.spotcheck.safety import (
    ALLOWED_PORTS,
    SafetyError,
    check_accounts,
    check_client_id,
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
    "send", "sendMsg", "__dict__", "attrgetter", "__getattribute__", "methodcaller", "getmembers",
}  # fmt: skip
ORDER_CALLS = {"__import__", "import_module", "vars", "attrgetter", "methodcaller", "getmembers"}
BAD_MODULES = (
    "bmm.gateway", "bmm.quoter", "bmm.hedger", "bmm.risk", "bmm.pricing.quoting", "ib_async.order",
)  # fmt: skip


# One test per guard; one case per distinct failure mode (Tier B: no look-alike sprawl).
@pytest.mark.parametrize(
    ("port", "ok"),
    [(4002, True), (4001, False), (4002.0, False), (True, False)],
    ids=["paper", "live_gateway", "float_spoof", "bool_spoof"],
)
def test_check_port(port, ok):
    if ok:
        assert ALLOWED_PORTS == frozenset({4002}) and check_port(port) == port
    else:
        with pytest.raises(SafetyError):
            check_port(port)


@pytest.mark.parametrize(
    ("env", "ok"),
    [({}, True), ({"BMM_MODE": " Record "}, True), ({"BMM_MODE": "live"}, False),
     ({"BMM_MODE": " LIVE\n"}, False), ({"BMM_MODE": "prod"}, False)],
    ids=["unset", "record_normalised", "live", "live_normalised", "unknown"],
)  # fmt: skip
def test_check_mode(env, ok):
    if ok:
        check_mode(env)
    else:
        with pytest.raises(SafetyError):
            check_mode(env)


@pytest.mark.parametrize(
    ("host", "allow", "ok"),
    [("127.0.0.1", None, True), ("::1", None, True), ("10.0.0.5", None, False),
     ("127.0.0.1.evil.com", None, False), ("10.0.0.5", ["10.0.0.5"], True)],
    ids=["loopback_v4", "loopback_v6", "remote", "loopback_prefix_spoof", "allowlisted_remote"],
)  # fmt: skip
def test_check_host(host, allow, ok):
    call = (lambda: check_host(host)) if allow is None else (lambda: check_host(host, allow=allow))
    if ok:
        assert call() == host
    else:
        with pytest.raises(SafetyError):
            call()


@pytest.mark.parametrize(
    ("cid", "ok"),
    [(31, True), (0, False), (-3, False), (True, False)],
    ids=["positive", "zero", "negative", "bool_spoof"],
)
def test_check_client_id(cid, ok):
    if ok:
        assert check_client_id(cid) == cid
    else:
        with pytest.raises(SafetyError):
            check_client_id(cid)


def test_refused_accounts_are_masked_in_the_message():
    with pytest.raises(SafetyError) as e:
        check_accounts(["U1234567", "DU7654321"])
    msg = str(e.value)
    assert "U***4567" in msg and "U1234567" not in msg and "DU7654321" not in msg


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
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ORDER_CALLS:
                    hits.append(f"{where}:dynamic attr call {node.func.attr}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ORDER_CALLS:
                    hits.append(f"{where}:dynamic import")
                if node.func.id in {"getattr", "setattr", "eval", "exec"}:
                    lit = len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                    if (
                        node.func.id != "getattr"
                        or not lit
                        or "order" in str(node.args[1].value).lower()
                    ):
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


EVASIONS = {  # one representative per scan rule
    "regex_name_spacing": "o = LimitOrder (1)",
    "getattr_non_literal": "x = getattr(ib, name)",
    "alias_import": "from ib_async import LimitOrder as LO",
    "star_import": "from ib_async import *",
    "order_module": "from ib_async.order import MarketOrder",
    "pricing_pkg": "from bmm.pricing import black76_call",
    "importlib": "import importlib",
    "dunder_import": '__import__("ib_async")',
    "forbidden_attr": "ib.client.send(1, 2)",
    "forbidden_call": "vars(ib)['x']",
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
