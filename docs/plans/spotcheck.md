# Spotcheck (venue binaries vs options surface) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Implementer subagent: `implementer`. Reviews: `risk-auditor` (Tasks 2, 7, 9, 10), `quant-reviewer` (Tasks 4, 5, 6, 11, 12, 13).

**Goal:** Build `bmm spotcheck collect|report`. The collector takes market-data-only snapshots from IB Gateway on paper port 4002 (venue ladders, reference futures, matched options chains) and writes them to parquet. The report turns 2–3 sessions into a GO/INVESTIGATE/KILL verdict per product, using the owner's decision rule.

**Architecture:** Pure, tested functions (normalisation, contract and expiry resolution, vol and strike window, metrics, rule) sit behind a `MarketDataSource` protocol. There are two implementations: `IBSource` (ib_async, read-only connection, port allowlist {4002}) and `FixtureSource` (deterministic synthetic session for dry runs and tests). The collector's clock and sleep are injected, so a full session replays in seconds. The report reads parquet, reuses the existing `analyze.edges` and `analyze.fair_band`, and renders markdown plus PNGs.

**Tech Stack:** Python 3.12, asyncio, ib_async, polars/pyarrow, scipy (brentq), matplotlib (new dependency), pytest/hypothesis.

**Spec:** `docs/spotcheck_spec.md` (moved from `spotcheck-addon/docs/` in Task 0). Also binding: `CLAUDE.md`, `SPEC.md` §3, `research/venue_facts.md`, `research/owner_checklist.md`.

## Decisions resolved during brainstorming

Owner answers (2026-10-04):
- **O1 Products.** Collect four products: `forecastex-spx` (FES), `forecastex-ndx` (FNQ), `forecastex-gold` (METLG, added) and `cme-gold` (ECGC).
- **O2 Decision-rule reading:**
  1. Median |gap| and the "% > 3¢" share are computed over (snapshot, strike) observations within a moneyness bucket.
  2. Persistence is the median run length of consecutive >3¢ snapshots at a strike.
  3. A snapshot's taker edge is its best strike's edge after fees.
  4. Precedence is **KILL → GO → INVESTIGATE**. GO drops to INVESTIGATE if it holds only when expiry-mismatched snapshots are included.
  5. Fees are run at 0.01 and 0.02 per side. The verdict uses 0.02 and the report shows both.

Resolved from the spec and Phase 0 research (no owner input needed):
- **R1. CME payout is not hardcoded.** The spec says "$100 payout, $1 tick → divide by 100", but `venue_facts.md` (CME "Payout / tick" [V]) says $1.00 / $0.01 since 2025-12-08. `payout_scale` is a per-product config value (default 1.0 for `cme-gold`). Every contract's details (multiplier, minTick, priceMagnifier) are logged to `contracts.json`. Any normalised price outside [0, 1] aborts that product for the session with a `payout_scale mismatch` error rather than guessing.
- **R2. Reference futures for ECGC come from `ContractDetails.underConId`**, not a roll rule. The roll rule is unknown (owner item #17), and the exchange's own linkage is model-free.
- **R3. FES/FNQ reference month** follows the [V] rule: the lead quarterly month, or the second month on the day the lead goes through final settlement (`venue_facts.md` S&P/NDX reference).
- **R4. METLG reference month** is "the Gold Futures contract named in the question". It is parsed from the ForecastEx contract's `longName`/`description`. If parsing fails, the owner must pass `--gc-month YYYYMM`, and the product is skipped, never guessed.
- **R5. Fix time (T anchor)** is the CME settlement on the question date, not resolution (owner item #20). Config defaults use the [V] last-trade times: FES/FNQ 15:00 CT, METLG/ECGC 12:30 CT. The exact settlement clock is owner item #7, so these are config values. Each ForecastEx contract's `lastTradeTime` is logged and compared, and a mismatch is a logged warning.
- **R6. Cadence is relative to each product's fix time.** Snapshots run every 5 min until fix−30m, then every 1 min until fix+30m, then the collector stops. For ES/NQ this is exactly the spec's 2:30–3:30 PM CT window. Gold gets the equivalent window around 12:30 CT. The owner starts the collector, so there's no guessed "venue trading hours".
- **R7. Expiry matching** only considers options whose `underConId` equals the reference future. An exact match means |option expiry − fix| ≤ 60 s. Otherwise the collector records the nearest expiries below and above the fix, with `expiry_mismatch=true`.
- **R8. σ for the ±4σ strike window and moneyness buckets** is the ATM implied vol from the matched chain, solved with brentq on `black76_call` and computed in two passes: first the ATM ±5 strikes, then the full window.
- **R9. Supporting modules.** `bmm/clock.py` and `bmm/cli.py` don't exist yet. This plan creates a minimal `clock.py` (CLAUDE.md requires it) and a `cli.py` with only the `spotcheck` subcommand. There is no `gateway/` module, because the spotcheck owns a market-data-only connector inside `spotcheck/`; Phase 1 builds the real gateway.
- **R10. The fixture is synthetic** (no recorded data exists yet). A seeded generator is built to exercise all three verdicts.
- **R11. Parquet layout follows the spec** (`data/spotcheck/date=YYYY-MM-DD/{ladder,futures,options}.parquet`). Each snapshot is appended by an atomic rewrite (write tmp, then `os.replace`). Files are small, under about 1 MB per day.
- **R12. Market-data lines.** Tickers are requested with `reqTickersAsync` (non-regulatory snapshots) in batches of `max_lines` (default 90). The live account's line allowance is owner-dependent.
- **R13. Paper and FORECASTX.** Whether FORECASTX data is served on a paper login is unknown (owner item: Paper trading support). The collector logs `no FORECASTX data on paper` per product and continues with the other products.

## Global Constraints

- **Market data only.** No order code may be imported or called anywhere under `src/bmm/spotcheck/` or the spotcheck path in `src/bmm/cli.py`. A test greps for `placeOrder|cancelOrder|reqGlobalCancel|whatIfOrder|bracketOrder|Order\(` and for `ib_async` imports of any name ending in `Order`.
- **Port allowlist is exactly {4002}.** 4001, 7496, 7497 and everything else are refused before any socket is opened. Connect with `readonly=True`. Refuse to start if `BMM_MODE=live`. No `--live` flag exists.
- Never edit `config/limits.toml` or `config/live.toml`. Never read `.env`.
- **Do not modify `src/bmm/spotcheck/analyze.py`** except to fix a failing test, and if so, show the diff. Task 0 records its sha256, and Task 14 re-checks it.
- **Decision-rule constants are owner-owned.** They are pinned verbatim in `rule.py` by a test. Agents may not change the values.
- Timestamps are int ns UTC from `bmm.clock`. Never call `datetime.now()` directly.
- Prices stay floats in [0, 1] per $1 payout. No rounding in data paths.
- ForecastEx: YES=C, NO=P, buy-only. `yes_bid = best YES bid`, `yes_ask = 1 − best NO bid`, `bid_size = YES bid size`, `ask_size = NO bid size`. Raw YES and NO bid/ask/sizes are always logged.
- Fees: ForecastEx 0.01 per side ([V] rules03242641956). CME EC exchange fee 0.01 per contract ([V] venue_facts "Fees"). IBKR commission is unknown (owner item #3), so every run uses fee_per_side ∈ {0.01, 0.02}.
- Money and probability math in tests: `pytest.approx` with explicit `abs=`.
- `make check` must stay green. Add `src/bmm/spotcheck` and `src/bmm/clock.py` to the mypy target (non-strict).

## Review Focus

1. **Missing quotes arrive as `nan`, `-1` or size 0 from ib_async.** They must become `None`, never price 0 or 1, or they create phantom 50¢ "gaps". Owned by Task 4 (`test_missing_quotes_become_none`).
2. **Crossed or locked venue books after normalisation** (YES bid + NO bid ≥ 1). Keep the row, flag `crossed=True`, and exclude it from gap stats while counting it in ladder quality. Owned by Tasks 4 and 11 (`test_crossed_row_flagged`, `test_crossed_rows_excluded_from_gap_stats`).
3. **DST.** Sessions will span 2026-11-01 (end of US DST). The CT fix time must convert to the correct UTC ns on both sides of it. Owned by Task 1 (`test_ct_to_ns_across_dst_end`).
4. **One product failing** (pacing error, no subscription, no ladder on a holiday, payout-scale mismatch) must not stop the others or crash the session. Owned by Task 9 (`test_product_failure_isolated`).
5. **No option expiry for the reference future**, or only one side of the fix. Return a mismatch with the available side, never an exception or a wrong-underlying chain. Owned by Task 5 (`test_one_sided_bracket`, `test_ignores_other_underlyings`).

## File structure

```
src/bmm/clock.py                     now_ns(), ct_to_ns(), ns_to_ct()            (Task 1)
src/bmm/spotcheck/analyze.py         EXISTING — do not modify
src/bmm/spotcheck/safety.py          ALLOWED_PORTS, check_port(), check_mode()    (Task 2)
src/bmm/spotcheck/schema.py          row dataclasses, polars schemas, ParquetSink (Task 3)
src/bmm/spotcheck/normalise.py       RawQuote, forecastex_row(), cme_row()        (Task 4)
src/bmm/spotcheck/contracts.py       ProductSpec, PRODUCTS, lead_month(), parse_gc_month(),
                                     ExpiryMatch, match_expiry(), expiry_ts_ns()  (Task 5)
src/bmm/spotcheck/vol.py             implied_vol(), atm_sigma(), strike_window()  (Task 6)
src/bmm/spotcheck/source.py          MarketDataSource Protocol + DTOs             (Task 7)
src/bmm/spotcheck/ibsource.py        IBSource (ib_async, read-only)               (Task 7)
src/bmm/spotcheck/fixture.py         FixtureSource + make_session()               (Task 8)
src/bmm/spotcheck/collect.py         schedule(), collect()                        (Task 9)
src/bmm/cli.py                       `bmm spotcheck collect|report`               (Task 10)
src/bmm/spotcheck/metrics.py         frame building, buckets, stats               (Task 11)
src/bmm/spotcheck/rule.py            pinned constants, verdict()                  (Task 12)
src/bmm/spotcheck/report.py          markdown + PNG writer                        (Task 13)
config/spotcheck.example.toml        product config (payout_scale, fix times, ib symbols)
tests/spotcheck/test_*.py            one file per module
```

---

### Task 0: Git bootstrap + import the addon

**Files:**
- Move: `spotcheck-addon/src/bmm/spotcheck/` → `src/bmm/spotcheck/`; `spotcheck-addon/tests/test_spotcheck.py` → `tests/spotcheck/test_analyze.py`; `spotcheck-addon/docs/spotcheck_spec.md` → `docs/spotcheck_spec.md`
- Create: `tests/spotcheck/__init__.py` (empty), `.git/`
- Modify: `Makefile` (mypy target)

- [ ] **Step 1: Write the checks first**

```bash
test -f src/bmm/spotcheck/analyze.py && test -f tests/spotcheck/test_analyze.py && test -f docs/spotcheck_spec.md && echo moved
git rev-parse --abbrev-ref HEAD          # expect: spotcheck
git ls-files | grep -cE '(^|/)\.env$'    # expect: 0
```
Run now. Expected: fails (not moved, not a git repo).

- [ ] **Step 2: Init the repo and commit the current state (including Phase 0 research)**

```bash
git init -b main
git check-ignore -q .env || { echo "STOP: .env not ignored"; exit 1; }
git add -A -- . ':!spotcheck-addon' && git commit -m "chore: initial import incl. Phase 0 research"
git switch -c spotcheck
```

- [ ] **Step 3: Move the addon and record the analyze.py hash**

```bash
mkdir -p tests/spotcheck && touch tests/spotcheck/__init__.py
git mv -k spotcheck-addon/src/bmm/spotcheck src/bmm/spotcheck 2>/dev/null || mv spotcheck-addon/src/bmm/spotcheck src/bmm/spotcheck
mv spotcheck-addon/tests/test_spotcheck.py tests/spotcheck/test_analyze.py
mv spotcheck-addon/docs/spotcheck_spec.md docs/spotcheck_spec.md
rmdir -p spotcheck-addon/src/bmm spotcheck-addon/tests spotcheck-addon/docs 2>/dev/null; ls spotcheck-addon 2>&1
shasum -a 256 src/bmm/spotcheck/analyze.py | tee docs/plans/spotcheck.analyze.sha256
```
Expected: `ls spotcheck-addon` reports no such directory.

- [ ] **Step 4: Extend the mypy target.** In `Makefile`, change `uv run mypy src/bmm/pricing` to `uv run mypy src/bmm/pricing src/bmm/spotcheck`.

- [ ] **Step 5: Run `make check`.** Expected: all 4 analyze tests plus the existing pricing tests pass, ruff is clean, mypy is clean. If mypy flags `analyze.py` (e.g. the `clip` lambda), add a per-module `[[tool.mypy.overrides]] module="bmm.spotcheck.analyze"` with `disable_error_code=[...]` in `pyproject.toml` rather than editing analyze.py.

- [ ] **Step 6: Commit** `git add -A && git commit -m "spotcheck: import addon (analyze.py unchanged)"`

---

### Task 1: `bmm.clock`

**Files:** Create `src/bmm/clock.py`, `tests/test_clock.py`

**Interfaces — Produces:**
```python
def now_ns() -> int                                   # UTC ns (time.time_ns)
def ct_to_ns(d: date, hh: int, mm: int, ss: int = 0) -> int   # America/Chicago wall time -> UTC ns
def ns_to_ct(ns: int) -> datetime                     # tz-aware America/Chicago
```

- [ ] **Step 1: Write failing tests**

```python
from datetime import date
import time
from bmm.clock import ct_to_ns, now_ns, ns_to_ct

def test_now_ns_is_utc_int():
    a = now_ns(); b = time.time_ns()
    assert isinstance(a, int) and abs(b - a) < 1_000_000_000

def test_ct_to_ns_across_dst_end():
    # CDT (UTC-5) on 2026-10-30, CST (UTC-6) on 2026-11-02
    assert ct_to_ns(date(2026, 10, 30), 15, 0) == 1_793_390_400_000_000_000  # 20:00Z
    assert ct_to_ns(date(2026, 11, 2), 15, 0) == 1_793_653_200_000_000_000   # 21:00Z

def test_roundtrip():
    ns = ct_to_ns(date(2026, 11, 2), 12, 30)
    t = ns_to_ct(ns)
    assert (t.hour, t.minute, t.tzinfo.key) == (12, 30, "America/Chicago")
```

- [ ] **Step 2: Run** `uv run pytest tests/test_clock.py -q`. Expected: ImportError.
- [ ] **Step 3: Implement**

```python
"""Single clock for bmm: all timestamps are int ns UTC."""
from __future__ import annotations
import time
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

def now_ns() -> int:
    return time.time_ns()

def ct_to_ns(d: date, hh: int, mm: int, ss: int = 0) -> int:
    return int(datetime.combine(d, dtime(hh, mm, ss), tzinfo=CT).timestamp()) * 1_000_000_000

def ns_to_ct(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=CT)
```
- [ ] **Step 4: Run tests.** Expected: 3 passed. (If an expected constant is off, recompute it with `datetime(..., tzinfo=timezone.utc)` and fix the **test**, showing the arithmetic in the commit message.)
- [ ] **Step 5: Commit** `git commit -am "feat(clock): ns UTC clock with CT helpers"` (add the new files first).

---

### Task 2: Safety guards + no-order test

**Files:** Create `src/bmm/spotcheck/safety.py`, `tests/spotcheck/test_safety.py`

**Interfaces — Produces:**
```python
ALLOWED_PORTS: frozenset[int] = frozenset({4002})
class SafetyError(RuntimeError): ...
def check_port(port: int) -> int        # returns port or raises SafetyError
def check_mode(env: Mapping[str, str]) -> None   # raises if env.get("BMM_MODE") == "live"
```

- [ ] **Step 1: Write failing tests**

```python
import ast, pathlib, re
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
    check_mode({"BMM_MODE": "record"}); check_mode({})

def _files():
    files = sorted(SPOT.rglob("*.py"))
    cli = ROOT / "src/bmm/cli.py"
    return files + ([cli] if cli.exists() else [])

def test_no_order_calls_in_spotcheck():
    hits = [f"{p}:{i}" for p in _files()
            for i, line in enumerate(p.read_text().splitlines(), 1) if FORBIDDEN.search(line)]
    assert hits == []

def test_no_order_imports_from_ib_async():
    bad = []
    for p in _files():
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("ib_async"):
                bad += [f"{p}:{a.name}" for a in node.names if a.name.endswith("Order") or a.name == "*"]
    assert bad == []

def test_guard_test_catches_a_violation(tmp_path):
    assert FORBIDDEN.search("ib.placeOrder(c, LimitOrder('BUY', 1, 0.5))")
```

- [ ] **Step 2: Run.** Expected: ImportError.
- [ ] **Step 3: Implement** `safety.py`:

```python
"""Hard guards for the market-data-only spotcheck (SPEC §3 rules 1-2)."""
from __future__ import annotations
from collections.abc import Mapping

ALLOWED_PORTS: frozenset[int] = frozenset({4002})  # IB Gateway paper. Never 4001.

class SafetyError(RuntimeError):
    pass

def check_port(port: int) -> int:
    if port not in ALLOWED_PORTS:
        raise SafetyError(f"spotcheck refuses port {port}; allowed: {sorted(ALLOWED_PORTS)}")
    return port

def check_mode(env: Mapping[str, str]) -> None:
    if env.get("BMM_MODE", "").lower() == "live":
        raise SafetyError("spotcheck is market-data only and refuses BMM_MODE=live")
```
- [ ] **Step 4: Run tests.** Expected: all pass.
- [ ] **Step 5: risk-auditor review** of `safety.py` and the test. Attach the verdict to the task report.
- [ ] **Step 6: Commit** `feat(spotcheck): port allowlist, live-mode refusal, no-order guard test`

---

### Task 3: Schemas + parquet sink

**Files:** Create `src/bmm/spotcheck/schema.py`, `tests/spotcheck/test_schema.py`

**Interfaces — Produces:**
```python
LADDER_SCHEMA, FUTURES_SCHEMA, OPTIONS_SCHEMA: dict[str, pl.DataType]
class ParquetSink:
    def __init__(self, root: Path) -> None
    def append(self, day: date, table: Literal["ladder","futures","options"], rows: list[dict[str, Any]]) -> Path
    def write_contracts(self, day: date, product: str, details: list[dict[str, Any]]) -> Path   # contracts.json, merged per product
```
Columns (exact names):
- **ladder:** `snapshot_id:str, ts_ns:i64, product:str, question_date:date, fix_ts_ns:i64, strike:f64, yes_bid:f64?, yes_ask:f64?, bid_size:i64, ask_size:i64, crossed:bool, raw_yes_bid:f64?, raw_yes_bid_size:i64, raw_yes_ask:f64?, raw_yes_ask_size:i64, raw_no_bid:f64?, raw_no_bid_size:i64, raw_no_ask:f64?, raw_no_ask_size:i64, last:f64?, last_size:i64, volume:f64?, payout_scale:f64`
- **futures:** `snapshot_id, ts_ns, product, con_id:i64, local_symbol:str, contract_month:str, bid:f64?, ask:f64?, bid_size:i64, ask_size:i64, last:f64?`
- **options:** `snapshot_id, ts_ns, product, underlying_con_id:i64, expiry_ts_ns:i64, expiry_role:str ("match"|"below"|"above"), expiry_mismatch:bool, strike:f64, call_bid:f64?, call_ask:f64?, und_px:f64?, atm_sigma:f64?`

- [ ] **Step 1: Write failing tests**: (a) golden schema, where `pl.DataFrame(schema=LADDER_SCHEMA).schema` equals a literal dict written in the test (this is the golden file); (b) `append` twice and read back, giving 2× rows, dtypes per schema, and the path `root/date=2026-10-05/ladder.parquet`; (c) `append` with a missing column raises `ValueError`; (d) atomicity, where after `append` no `*.tmp` files remain.
- [ ] **Step 2: Run.** Expected: ImportError.
- [ ] **Step 3: Implement** with polars. `append` reads the existing file if present, `pl.concat`s, writes `<file>.tmp`, then `os.replace`. Validate the column set equals the schema keys exactly.
- [ ] **Step 4: Run.** Expected: pass.
- [ ] **Step 5: Commit** `feat(spotcheck): parquet schemas and atomic sink`

---

### Task 4: Normalisation

**Files:** Create `src/bmm/spotcheck/normalise.py`, `tests/spotcheck/test_normalise.py`

**Interfaces — Consumes:** `LadderRow` from `analyze.py`. **Produces:**
```python
@dataclass(frozen=True)
class RawQuote:                       # one IB ticker, already in venue price units
    bid: float | None; ask: float | None; bid_size: int; ask_size: int
    last: float | None = None; last_size: int = 0; volume: float | None = None
def clean(px: float | None) -> float | None          # nan / <= 0 / -1 -> None
def raw_quote(bid, ask, bid_size, ask_size, last=None, last_size=0, volume=None) -> RawQuote  # applies clean() + size floor 0
@dataclass(frozen=True)
class NormRow:  row: LadderRow; crossed: bool; raw_yes: RawQuote; raw_no: RawQuote | None
def forecastex_row(strike: float, yes: RawQuote, no: RawQuote) -> NormRow
def cme_row(strike: float, q: RawQuote, payout_scale: float) -> NormRow
class ScaleError(ValueError): ...
```

- [ ] **Step 1: Write failing tests**

```python
import math
import pytest
from bmm.spotcheck.normalise import ScaleError, cme_row, forecastex_row, raw_quote

def test_forecastex_yes_ask_is_one_minus_no_bid():
    r = forecastex_row(6500.0, raw_quote(0.47, None, 30, 0), raw_quote(0.50, None, 25, 0))
    assert r.row.yes_bid == pytest.approx(0.47, abs=1e-12)
    assert r.row.yes_ask == pytest.approx(0.50, abs=1e-12)
    assert (r.row.bid_size, r.row.ask_size) == (30, 25)
    assert r.raw_no.bid == 0.50 and not r.crossed

def test_missing_quotes_become_none():
    r = forecastex_row(6500.0, raw_quote(math.nan, -1.0, 0, 0), raw_quote(-1.0, math.nan, 0, 0))
    assert r.row.yes_bid is None and r.row.yes_ask is None
    assert r.row.bid_size == 0 and r.row.ask_size == 0

def test_crossed_row_flagged():
    r = forecastex_row(6500.0, raw_quote(0.52, None, 5, 0), raw_quote(0.50, None, 5, 0))
    assert r.crossed  # yes_bid 0.52 >= yes_ask 0.50

def test_cme_scale_1_and_100():
    a = cme_row(4000.0, raw_quote(0.41, 0.44, 10, 12), payout_scale=1.0)
    b = cme_row(4000.0, raw_quote(41.0, 44.0, 10, 12), payout_scale=100.0)
    assert a.row.yes_bid == pytest.approx(b.row.yes_bid, abs=1e-12) == pytest.approx(0.41, abs=1e-12)
    assert a.row.yes_ask == pytest.approx(0.44, abs=1e-12) and a.row.ask_size == 12

def test_cme_scale_mismatch_raises():
    with pytest.raises(ScaleError):
        cme_row(4000.0, raw_quote(41.0, 44.0, 1, 1), payout_scale=1.0)
```
- [ ] **Step 2: Run.** Expected: ImportError.
- [ ] **Step 3: Implement.**
  - `clean`: return None if px is None, NaN, or ≤ 0.
  - `forecastex_row`: `yes_ask = None if no.bid is None else 1.0 - no.bid`; `crossed = yes_bid is not None and yes_ask is not None and yes_bid >= yes_ask`.
  - `cme_row`: divide prices by `payout_scale`, and raise `ScaleError` if any result is > 1.0.
- [ ] **Step 4: Run.** Expected: pass.
- [ ] **Step 5: quant-reviewer** on the normalisation (buy-only translation, sizes, crossed semantics). Attach the verdict.
- [ ] **Step 6: Commit** `feat(spotcheck): ForecastEx/CME ladder normalisation`

---

### Task 5: Products, reference month, expiry matching

**Files:** Create `src/bmm/spotcheck/contracts.py`, `config/spotcheck.example.toml`, `tests/spotcheck/test_contracts.py`

**Interfaces — Produces:**
```python
@dataclass(frozen=True)
class ProductSpec:
    key: str; venue: Literal["forecastex", "cme"]; ib_symbol: str; ib_sec_type: str; ib_exchange: str
    trading_class: str | None; fut_symbol: str; fut_exchange: str; opt_exchange: str
    fix_ct: tuple[int, int]; payout_scale: float; ref_rule: Literal["lead_quarterly", "named_gc", "under_con_id"]
PRODUCTS: dict[str, ProductSpec]   # keys: forecastex-spx, forecastex-ndx, forecastex-gold, cme-gold
def load_products(path: Path | None) -> dict[str, ProductSpec]     # TOML overrides on defaults
def lead_month(question_date: date, final_settlement_dates: list[date]) -> date
def parse_gc_month(text: str) -> str | None                         # -> "YYYYMM"
def expiry_ts_ns(yyyymmdd: str, hhmm: str, tz_id: str) -> int
@dataclass(frozen=True)
class OptExpiry: expiry_ts_ns: int; underlying_con_id: int; trading_class: str; yyyymmdd: str
@dataclass(frozen=True)
class ExpiryMatch: chosen: list[tuple[OptExpiry, Literal["match","below","above"]]]; mismatch: bool
def match_expiry(fix_ts_ns: int, ref_con_id: int, expiries: list[OptExpiry], tol_ns: int = 60_000_000_000) -> ExpiryMatch | None
```
Defaults:
- `forecastex-spx`: ib_symbol `FES`, OPT/FORECASTX, fut `ES`/CME, opt exchange CME, fix (15, 0), scale 1.0, rule `lead_quarterly`.
- `forecastex-ndx`: `FNQ`, fut `NQ`, otherwise the same as spx.
- `forecastex-gold`: `METLG`, fut `GC`/COMEX, opt COMEX, fix (12, 30), rule `named_gc`.
- `cme-gold`: FOP/COMEX, trading_class `ECGC`, ib_symbol `GC`, fix (12, 30), scale 1.0, rule `under_con_id`.
- The example TOML mirrors these with a comment per value giving its venue_facts source, and notes that ib_symbol mappings are to be confirmed on first connect.

- [ ] **Step 1: Write failing tests**

```python
from datetime import date
from bmm.clock import ct_to_ns
from bmm.spotcheck.contracts import OptExpiry, PRODUCTS, lead_month, match_expiry, parse_gc_month

QTR = [date(2026, 12, 18), date(2027, 3, 19), date(2027, 6, 18)]

def test_lead_month_normal_day():
    assert lead_month(date(2026, 10, 5), QTR) == date(2026, 12, 18)

def test_lead_month_rolls_on_final_settlement_day():
    assert lead_month(date(2026, 12, 18), QTR) == date(2027, 3, 19)

def test_parse_gc_month():
    assert parse_gc_month("Gold Futures Dec 2026 settle above 2650?") == "202612"
    assert parse_gc_month("COMEX GC FEB27") == "202702"
    assert parse_gc_month("Gold price question") is None

FIX = ct_to_ns(date(2026, 10, 5), 15, 0)
def ox(ns, con=111): return OptExpiry(ns, con, "E1A", "20261005")

def test_exact_match():
    m = match_expiry(FIX, 111, [ox(FIX + 30_000_000_000), ox(FIX + 86_400_000_000_000)])
    assert m and not m.mismatch and [r for _, r in m.chosen] == ["match"]

def test_bracketing_mismatch():
    m = match_expiry(FIX, 111, [ox(FIX - 3_600_000_000_000), ox(FIX + 7_200_000_000_000)])
    assert m.mismatch and [r for _, r in m.chosen] == ["below", "above"]

def test_one_sided_bracket():
    m = match_expiry(FIX, 111, [ox(FIX + 7_200_000_000_000)])
    assert m.mismatch and [r for _, r in m.chosen] == ["above"]

def test_ignores_other_underlyings():
    assert match_expiry(FIX, 111, [ox(FIX, con=222)]) is None

def test_products_fix_times():
    assert PRODUCTS["forecastex-spx"].fix_ct == (15, 0) and PRODUCTS["cme-gold"].fix_ct == (12, 30)
    assert set(PRODUCTS) == {"forecastex-spx", "forecastex-ndx", "forecastex-gold", "cme-gold"}
```
- [ ] **Step 2: Run.** Expected: ImportError.
- [ ] **Step 3: Implement.**
  - `lead_month`: `sorted(d for d in dates if d > question_date)[0]` if `question_date` is in `dates`; otherwise the first `d >= question_date`.
  - `parse_gc_month`: regex `\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s*'?(\d{2}|\d{4})\b`, case-insensitive.
  - `match_expiry`: filter to `underlying_con_id == ref_con_id`; return None if the list is empty; take the nearest with |Δ| ≤ tol as "match"; otherwise the last below and first above.
  - `expiry_ts_ns`: use `ZoneInfo(tz_id)` (IB `timeZoneId`, e.g. "US/Central").
- [ ] **Step 4: Run.** Expected: pass.
- [ ] **Step 5: quant-reviewer** on reference-month and expiry-matching logic (spec acceptance requires it). Attach the verdict.
- [ ] **Step 6: Commit** `feat(spotcheck): product specs, reference month, expiry matching`

---

### Task 6: Implied vol, ATM σ, strike window

**Files:** Create `src/bmm/spotcheck/vol.py`, `tests/spotcheck/test_vol.py`

**Interfaces — Consumes:** `black76_call` from `bmm.pricing`; `OptQuote` from `analyze`. **Produces:**
```python
def implied_vol(F: float, K: float, T: float, price: float) -> float | None   # brentq on [1e-4, 5]; None if price outside no-arb range
def atm_sigma(chain: list[OptQuote], F: float, T: float) -> float | None      # IV of mid at strike nearest F
def strike_window(F: float, sigma: float, T: float, n_sd: float = 4.0) -> tuple[float, float]  # F*exp(∓n_sd*sigma*sqrt(T))
def year_frac(from_ns: int, to_ns: int) -> float                              # (to-from)/(365*86400e9), floor 1e-9
```

- [ ] **Step 1: Write failing tests:**
  - `implied_vol(F, K, T, black76_call(F, K, T, 0.18)) == approx(0.18, abs=1e-6)` for K in {0.97F, F, 1.03F}.
  - Prices below intrinsic or ≥ F return None.
  - `atm_sigma` on a skewed chain (the `vol` lambda copied from `test_analyze.py`) ≈ `vol(F)` with abs=2e-3.
  - `strike_window(6500, 0.16, 1/252)` ≈ (6500·e^(−4·0.16·√(1/252)), 6500·e^(4·0.16·√(1/252))) with abs=1e-6.
  - `year_frac(0, 0) == 1e-9`.
- [ ] **Step 2: Run.** Expected: ImportError.
- [ ] **Step 3: Implement** with `scipy.optimize.brentq`.
- [ ] **Step 4: Run.** Expected: pass.
- [ ] **Step 5: quant-reviewer** (calendar-year T convention vs SPEC §6.3; bracket choice). Attach the verdict.
- [ ] **Step 6: Commit** `feat(spotcheck): implied vol and strike window`

---

### Task 7: Source protocol + IB adapter (read-only)

**Files:** Create `src/bmm/spotcheck/source.py`, `src/bmm/spotcheck/ibsource.py`, `tests/spotcheck/test_ibsource.py`

**Interfaces — Produces (`source.py`):**
```python
@dataclass(frozen=True)
class LadderQuote: strike: float; right: Literal["C", "P", "B"]; quote: RawQuote   # "B" = CME both-sided
@dataclass(frozen=True)
class FutQuote: con_id: int; local_symbol: str; contract_month: str; quote: RawQuote
class MarketDataSource(Protocol):
    async def ladder(self, spec: ProductSpec, question_date: date) -> tuple[list[LadderQuote], list[dict[str, Any]]]  # quotes, contract details log
    async def reference_future(self, spec: ProductSpec, question_date: date, ladder_details: list[dict[str, Any]], gc_month: str | None) -> FutQuote
    async def option_expiries(self, spec: ProductSpec, fut: FutQuote) -> list[OptExpiry]
    async def calls(self, spec: ProductSpec, expiry: OptExpiry, strikes: list[float]) -> list[OptQuote]
    async def listed_strikes(self, spec: ProductSpec, expiry: OptExpiry) -> list[float]
    async def close(self) -> None
class SourceError(RuntimeError): ...   # product-level failure (no data, no subscription, scale, unresolved month)
```
`ibsource.py`: `IBSource.connect(host: str, port: int, client_id: int, max_lines: int = 90, ib_factory=IB) -> IBSource`. It calls `check_port(port)` **before** constructing or connecting, then `await ib.connectAsync(host, port, clientId=client_id, readonly=True)`. Implementation notes:
- `ladder`:
  - ForecastEx: `reqContractDetailsAsync(Contract(secType="OPT", exchange="FORECASTX", symbol=spec.ib_symbol, lastTradeDateOrContractMonth=yyyymmdd))`, then `reqTickersAsync` in batches of `max_lines`.
  - CME: `Contract(secType="FOP", exchange=spec.ib_exchange, symbol=spec.ib_symbol, tradingClass=spec.trading_class, lastTradeDateOrContractMonth=yyyymmdd)`, calls only, right "B".
  - The details log is `{conId, localSymbol, right, strike, multiplier, minTick, priceMagnifier, longName, lastTradeTime, timeZoneId, underConId}`.
  - Empty details raise `SourceError("no ladder listed")`. A ticker with all-NaN quotes on every FORECASTX contract raises `SourceError("no FORECASTX data on paper")`.
- `reference_future`:
  - `lead_quarterly`: futures details → `lead_month` on `realExpirationDate`.
  - `named_gc`: `parse_gc_month` on the ladder `longName`s, then `gc_month`; if neither is available, raise `SourceError("METLG month unresolved; pass --gc-month")`.
  - `under_con_id`: the `underConId` from ladder details.
- `option_expiries`: `reqSecDefOptParamsAsync(spec.fut_symbol, spec.opt_exchange, "FUT", fut.con_id)` → per expiry within the next 7 days, `reqContractDetailsAsync` of one call → build `OptExpiry` from `underConId`, `lastTradeTime` and `timeZoneId` via `expiry_ts_ns`.
- `calls`: `FuturesOption(spec.fut_symbol, yyyymmdd, K, "C", spec.opt_exchange, tradingClass=expiry.trading_class)` → qualify → tickers in batches.
- **No other ib_async methods.**

- [ ] **Step 1: Write failing tests** with a `FakeIB` class recording calls:
  - `IBSource.connect("127.0.0.1", 4001, 7, ib_factory=FakeIB)` raises `SafetyError` and `FakeIB` is **never instantiated** (class-level counter == 0).
  - `connect(..., 4002, ...)` calls `connectAsync` with `readonly=True`.
  - Ladder with 3 strikes × C/P returns 6 `LadderQuote`s, and batching with `max_lines=4` makes 2 `reqTickersAsync` calls.
  - Ladder where all tickers are NaN raises `SourceError` containing "paper".
  - `named_gc` with longName "Gold Dec 2026" resolves contract_month "202612"; with no parse and no `gc_month`, it raises `SourceError`.
  - The `FakeIB` attribute-access log contains no name matching `/order/i`.
- [ ] **Step 2: Run.** Expected: ImportError.
- [ ] **Step 3: Implement** per the notes above.
- [ ] **Step 4: Run** the tests plus `tests/spotcheck/test_safety.py`. Expected: pass.
- [ ] **Step 5: risk-auditor review** of `ibsource.py` (port guard ordering, readonly, no order surface). Attach the verdict.
- [ ] **Step 6: Commit** `feat(spotcheck): read-only IB market-data source`

---

### Task 8: Synthetic fixture source

**Files:** Create `src/bmm/spotcheck/fixture.py`, `tests/spotcheck/test_fixture.py`

**Interfaces — Produces:** `FixtureSource(session: Session)` implementing `MarketDataSource`, and `make_session(question_date: date, seed: int = 7) -> Session`. Also `FixtureSource.from_json(path) / to_json(path)`, with the dry-run fixture checked in at `tests/fixtures/spotcheck/session.json`, generated by `uv run python -m bmm.spotcheck.fixture tests/fixtures/spotcheck/session.json`. The session covers the four products on question date 2026-10-05. Time is driven by an injected clock: the source returns quotes for the snapshot whose ts ≤ the current virtual time.

The fixture is designed so the report yields known verdicts:
- `forecastex-spx`: skewed Black-76 chain (vol lambda from test_analyze), F drifting ±3 pts. Ladder mids at fair except strikes in the 1–2σ upside bucket, which sit **+4¢ rich for 8 consecutive snapshots** with size 40. **Expected: GO.**
- `forecastex-ndx`: ladder mids within ±1¢ of fair with 4¢ wide markets. **Expected: KILL.**
- `forecastex-gold`: +4¢ gaps but size 3. **Expected: INVESTIGATE** (tiny size).
- `cme-gold`: +4¢ gaps with size 40, but the only option expiry is 1 day after the fix, so every snapshot is mismatched. **Expected: INVESTIGATE** (mismatch-only).
- Every product has quotes and 2 trades between fix and fix+30m (post-settlement activity), and one crossed row at one snapshot.

- [ ] **Step 1: Write failing tests:**
  - `make_session` is deterministic (same seed gives equal JSON).
  - The JSON round-trips.
  - `await src.ladder(PRODUCTS["forecastex-spx"], d)` returns C and P quotes.
  - `option_expiries` for cme-gold has no expiry within 60 s of the fix.
- [ ] **Step 2–4:** Implement, run, and pass. Generate and commit `session.json` (keep it under 2 MB by limiting strikes to ±4σ).
- [ ] **Step 5: Commit** `feat(spotcheck): deterministic synthetic session fixture`

---

### Task 9: Collector

**Files:** Create `src/bmm/spotcheck/collect.py`, `tests/spotcheck/test_collect.py`

**Interfaces — Consumes:** Tasks 3–8. **Produces:**
```python
def next_snapshot_ns(now_ns: int, fix_ts_ns: int) -> int | None    # 5-min grid until fix-30m, 1-min grid until fix+30m, else None
async def collect_once(source, spec, question_date, sink, snapshot_id, ts_ns, gc_month) -> None  # raises SourceError
async def collect(source: MarketDataSource, products: list[ProductSpec], question_date: date,
                  sink: ParquetSink, clock: Callable[[], int], sleep: Callable[[float], Awaitable[None]],
                  gc_month: str | None = None, log=structlog.get_logger()) -> dict[str, str]   # product -> "ok" | error text
```
`collect_once` does, for one product:
1. Ladder → normalise. ForecastEx pairs C/P by strike; CME uses `cme_row` with `spec.payout_scale`.
2. Reference future.
3. `match_expiry`; if None, write ladder + futures and skip options with `expiry_mismatch` unset, logging `no options for reference future`.
4. For each chosen expiry: listed strikes → ATM ±5 calls → `atm_sigma` → `strike_window` → calls in window.
5. Write all three tables with the same `snapshot_id` (`f"{product}-{ts_ns}"`) and `ts_ns` from `clock()`.
6. Write the contract-details log on the first snapshot per product, and warn if the ForecastEx `lastTradeTime` ≠ `spec.fix_ct`.

`collect` loops on the earliest `next_snapshot_ns` across products. Each product is wrapped in `try/except (SourceError, ScaleError, asyncio.TimeoutError, ConnectionError)`; a failed product records its error, is retried at its next slot up to 3 consecutive failures, and is then dropped for the session. The loop ends when every product returns None.

- [ ] **Step 1: Write failing tests**
  - `next_snapshot_ns`, with fix = 15:00 CT:
    - from 10:02 CT the next slot is 10:05;
    - from 14:31:10 it is 14:32;
    - from 15:29:30 it is 15:30;
    - from 15:30:01 it is None.
  - Dry run: `collect(FixtureSource(...), all 4 products, ...)` with a virtual clock (sleep advances it) writes `ladder/futures/options.parquet` under `tmp/date=2026-10-05/`, and every product status is "ok". Ladder rows per snapshot equal the fixture's strike count. `options.expiry_mismatch` is all True for cme-gold and all False for forecastex-spx.
  - `test_product_failure_isolated`: a source raising `SourceError` for forecastex-gold leaves the other 3 "ok" and gives `status["forecastex-gold"]` containing the error.
  - A scale-mismatch product (cme-gold with payout_scale overridden to 0.01) is reported as a failure without crashing.
- [ ] **Step 2: Run.** Expected: ImportError.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: pass. The fixture dry run completes in < 10 s wall time.
- [ ] **Step 5: risk-auditor** (no order surface, failure isolation, stale/missing data handling) **and quant-reviewer** (normalisation wiring and expiry matching, as the spec's /goal requires). Attach both verdicts.
- [ ] **Step 6: Commit** `feat(spotcheck): snapshot collector with fixture dry run`

---

### Task 10: CLI

**Files:** Create `src/bmm/cli.py`, `tests/test_cli.py`. Modify `pyproject.toml` (add `[project.scripts] bmm = "bmm.cli:main"`).

**Interfaces — Produces:** `main(argv: list[str] | None = None) -> int`.
- `bmm spotcheck collect --product forecastex-spx,forecastex-ndx,forecastex-gold,cme-gold [--question-date YYYY-MM-DD (default: today CT)] [--host 127.0.0.1] [--port 4002] [--client-id 31] [--out data/spotcheck] [--config config/spotcheck.toml] [--gc-month YYYYMM] [--fixture PATH]`
- `bmm spotcheck report --from YYYY-MM-DD --to YYYY-MM-DD [--data data/spotcheck] [--out reports] [--name NAME]`

Behaviour:
- `check_mode(os.environ)` runs first, then `check_port(args.port)`, both before any import of `ibsource`.
- `--fixture` uses `FixtureSource` with a virtual clock and never touches the network.
- Exit code 0 when ≥ 1 product is ok, 2 when all failed, and 3 on a `SafetyError`.

- [ ] **Step 1: Write failing tests:**
  - `main(["spotcheck","collect","--port","4001","--product","forecastex-spx"]) == 3`, and `bmm.spotcheck.ibsource` is not in `sys.modules` afterwards (use `monkeypatch.delitem(sys.modules, ..., raising=False)` first).
  - With `BMM_MODE=live` the result is 3.
  - `main(["spotcheck","collect","--fixture","tests/fixtures/spotcheck/session.json","--question-date","2026-10-05","--out",str(tmp)]) == 0`, and the three parquet files exist.
  - `"--live"` is rejected by argparse (`SystemExit` code 2).
  - `test_no_order_calls_in_spotcheck` now also scans `cli.py`.
- [ ] **Step 2–4:** Run (fail), implement, run (pass). Run `uv sync` and `uv run bmm spotcheck --help`.
- [ ] **Step 5: risk-auditor review** of `cli.py`. Attach the verdict.
- [ ] **Step 6: Commit** `feat(cli): bmm spotcheck collect|report`

---

### Task 11: Metrics

**Files:** Create `src/bmm/spotcheck/metrics.py`, `tests/spotcheck/test_metrics.py`

**Interfaces — Consumes:** parquet tables, `analyze.edges`, `analyze.OptQuote/LadderRow`, `vol.year_frac`. **Produces:**
```python
MONEYNESS_BUCKETS = ("<0.5", "0.5-1", "1-2", ">2")          # |ln K/F| / (sigma*sqrt T)
TTF_BUCKETS = (">4h", "1-4h", "30-60m", "<30m", "post-fix")
def load(data_root: Path, d_from: date, d_to: date) -> dict[str, pl.DataFrame]
def edge_frame(tables, fee_per_side: float) -> pl.DataFrame
    # one row per (snapshot, product, strike): EdgeRow fields + product, snapshot_id, ts_ns, fix_ts_ns,
    # F, sigma, T, m_bucket, ttf_bucket, expiry_mismatch, crossed, bid_size, ask_size
    # Uses only expiry_role=="match" options when present; else the "below"/"above" chain nearest the fix (flagged mismatch)
def moneyness_bucket(K, F, sigma, T) -> str
def ttf_bucket(ts_ns, fix_ts_ns) -> str
def gap_stats(ef) -> pl.DataFrame            # per product x m_bucket (and x ttf_bucket): n, median_abs_gap, iqr, pct_gt_2c, pct_gt_3c   (crossed rows excluded)
def taker_stats(ef) -> pl.DataFrame          # per product: pct_snapshots_best_edge_gt_1c / _gt_2c / _le_0, capturable $ per snapshot at 0.01/0.02 (median, p90)
def arb_table(ef, tables) -> pl.DataFrame    # rows with arb_*_edge > 0, plus option quote age/width check columns: opt_ts_lag_ms, opt_rel_width, suspect: bool
def persistence(ef, thr=0.03) -> pl.DataFrame  # per product x m_bucket: run lengths of consecutive >thr snapshots per strike; median_run, max_run
def ladder_quality(tables) -> pl.DataFrame   # per product: median width, median depth, strikes quoted, crossed count, volume delta, trades seen
def post_fix_activity(tables) -> pl.DataFrame  # per product: quotes resting after fix (count), trades after fix (volume delta), trade px vs last pre-fix fair
def mismatch_share(ef) -> pl.DataFrame       # per product: share mismatched, median gap matched vs mismatched (bias direction)
```
This is more than an hour of work, so split it into **11a** (load, edge_frame, buckets) and **11b** (the stats functions), committing each.

- [ ] **Step 1 (11a): Write failing tests:**
  - `moneyness_bucket(6500, 6500, 0.16, 1/252) == "<0.5"`, and a strike at 1.5σ gives `"1-2"`.
  - `ttf_bucket(fix + 1, fix) == "post-fix"`.
  - On a hand-built 2-snapshot table (one strike, chain from the test_analyze generator), `edge_frame` gives 2 rows with `mid_gap` equal to `analyze.edges(...)[0].mid_gap` (abs=1e-12).
  - `test_crossed_rows_excluded_from_gap_stats`: a crossed row exists in `ef` but is not counted in `gap_stats.n`.
- [ ] **Step 2 (11b): Write failing tests** on small literal frames:
  - persistence with gaps [4,4,4,1,4,4]¢ gives runs [3,2], median 2.5, max 3.
  - `pct_gt_3c` with 3 of 4 observations > 3¢ gives 0.75.
  - Taker: a snapshot's best edge is the max over strikes of max(buy, sell).
  - `arb_table` flags `suspect=True` when `opt_rel_width > 0.5` or the option quote is older than the ladder by > 5 s.
  - `post_fix_activity` counts fixture trades after the fix.
- [ ] **Step 3: Implement**, run, and pass. Commit 11a and 11b separately.
- [ ] **Step 4: quant-reviewer** on buckets, σ/T usage, persistence and the mismatch bias method. Attach the verdict.

---

### Task 12: Decision rule (owner-owned)

**Files:** Create `src/bmm/spotcheck/rule.py`, `tests/spotcheck/test_rule.py`

**Interfaces — Produces:**
```python
# OWNER-OWNED (docs/spotcheck_spec.md "Decision rule"). Agents must not change these values.
GO_MEDIAN_ABS_GAP = 0.03; GO_PCT_GT_3C = 0.30; GO_MIN_PERSISTENCE = 3; GO_MIN_MEDIAN_SIZE = 20
KILL_MEDIAN_ABS_GAP = 0.02; KILL_PCT_TAKER_LE_0 = 0.90; VERDICT_FEE = 0.02
@dataclass(frozen=True)
class Verdict: label: Literal["GO", "INVESTIGATE", "KILL"]; reasons: list[str]; numbers: dict[str, float]
def verdict(product: str, gap_all: pl.DataFrame, gap_matched_only: pl.DataFrame,
            persistence: pl.DataFrame, sizes: pl.DataFrame, taker: pl.DataFrame) -> Verdict
```
Logic (owner reading O2):
1. **KILL** if every bucket has median |gap| < 0.02, or `pct_snapshots_best_edge_le_0` > 0.90 at fee 0.02.
2. Otherwise **GO** if some bucket in `gap_matched_only` has median |gap| ≥ 0.03, `pct_gt_3c` ≥ 0.30, `median_run` ≥ 3, and median displayed size at its >3¢ strikes ≥ 20.
3. Otherwise **INVESTIGATE**. Reasons cover GO holding only with mismatched data, tiny size, or gaps without persistence.

`numbers` carries every value compared.

- [ ] **Step 1: Write failing tests:**
  - `test_constants_pinned` asserts each constant equals the literal above (the protection test).
  - A GO case, and a KILL case via each clause.
  - `test_kill_precedes_go`: GO conditions met but taker ≤ 0 in 95% of snapshots gives KILL.
  - `test_go_only_with_mismatch_is_investigate`.
  - `test_tiny_size_is_investigate`.
- [ ] **Step 2–4:** Run (fail), implement, run (pass).
- [ ] **Step 5: quant-reviewer.** Attach the verdict.
- [ ] **Step 6: Commit** `feat(spotcheck): owner decision rule (pinned)`

---

### Task 13: Report writer

**Files:** Create `src/bmm/spotcheck/report.py`, `tests/spotcheck/test_report.py`. Modify `pyproject.toml` (add `matplotlib>=3.8`). Wire into `cli.py`'s `report` subcommand.

**Interfaces — Produces:** `write_report(data_root: Path, d_from: date, d_to: date, out_dir: Path, name: str | None = None) -> Path`. It writes `out_dir/spotcheck_<name or from_to>.md` plus `out_dir/spotcheck_<name>_<product>_{gap_by_moneyness,persistence}.png`. Before writing chart code, load the `dataviz` skill and follow it (one palette, labelled axes in ¢, light/dark-safe colours).

The report starts with a header (dates, products, fee runs 0.01 and 0.02, the fee sources from Global Constraints, the owner-items caveat for the IBKR commission). Then, per product, exactly these H3 headings, in order:
1. `### 1. Ladder quality`
2. `### 2. Mid gap vs fair`
3. `### 3. Taker edge after fees`
4. `### 4. Model-free arb edges` (its own table if non-zero, including the `suspect` column)
5. `### 5. Persistence`
6. `### 6. Post-settlement window`
7. `### 7. Expiry mismatch`
8. `### 8. Verdict` (label in bold, reasons, and a table of the numbers that drove it)

It ends with `## Summary` (one line per product: verdict and the driving number).

- [ ] **Step 1: Write failing test:** run the fixture dry run (via `collect`), then `write_report(..., name="fixture")`.
  - Headings: `[l for l in md.splitlines() if l.startswith("### ")]` gives 8 per product × 4 products, in order.
  - Verdicts: spx **GO**, ndx **KILL**, forecastex-gold **INVESTIGATE**, cme-gold **INVESTIGATE**.
  - The PNGs exist and are > 1 KB.
  - Both fee runs appear in section 3.
- [ ] **Step 2–4:** Run (fail), implement, run (pass). If the fixture's verdicts don't come out as designed, fix the **fixture** (Task 8 generator), not the rule constants.
- [ ] **Step 5: quant-reviewer** on the report's computed numbers vs the spec's 8 sections. Attach the verdict.
- [ ] **Step 6: Commit** `feat(spotcheck): markdown/PNG report with verdict block`

---

### Task 14: Gate (the spec's /goal evidence)

**Files:** none (verification only)

- [ ] **Step 1: Run and print each item:**

```bash
make check
uv run pytest -q tests/spotcheck/test_safety.py tests/test_cli.py -k "order or port or live" -v
rm -rf data/spotcheck && uv run bmm spotcheck collect --fixture tests/fixtures/spotcheck/session.json --question-date 2026-10-05 --out data/spotcheck
ls -R data/spotcheck
uv run bmm spotcheck report --from 2026-10-05 --to 2026-10-05 --data data/spotcheck --out reports --name fixture
grep -E '^#{2,3} ' reports/spotcheck_fixture.md
shasum -a 256 -c docs/plans/spotcheck.analyze.sha256    # expect OK, otherwise show `git diff src/bmm/spotcheck/analyze.py`
git diff main --stat -- config/limits.toml config/live.toml   # expect empty
```
Expected:
- `make check` is green.
- The safety and port tests pass.
- `ls -R` shows `date=2026-10-05/{ladder,futures,options}.parquet` and `contracts.json`.
- The report has 8 sections × 4 products plus Summary.
- The analyze.py hash is OK.
- There is no diff on the limits or live config.

- [ ] **Step 2: Run `/review-order-path`**. risk-auditor covers `safety.py`, `ibsource.py`, `collect.py` and `cli.py`; quant-reviewer covers `normalise.py`, `contracts.py`, `vol.py`, `metrics.py`, `rule.py` and `report.py`. **quant-reviewer must PASS on collect's normalisation and expiry matching** (spec /goal).

- [ ] **Step 3: Hand-off note for the owner (print it, don't execute it).** The live collection command is `bmm spotcheck collect --product forecastex-spx,forecastex-ndx,forecastex-gold,cme-gold` on the VPS against paper port 4002. Before the first session, confirm the open owner items: #2 paper FORECASTX data, #3 IBKR commission, #5 data subscriptions, #7 settlement clocks (fix times in `config/spotcheck.toml`), and the IB symbols for FES/FNQ/METLG/ECGC.
