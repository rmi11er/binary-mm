# Risk audit (round 1): `spotcheck` branch (HEAD 55997fe, base `main`)

Reviewer: risk-auditor subagent · 2026-10-04 · read-only

**Verdict: CHANGES_REQUIRED** (0 Critical / 1 Important / 7 Minor)

I found no code path on this branch that can place, modify or cancel an order, and none that can reach port 4001, a non-loopback host it wasn't told to allow, client id 0, or `BMM_MODE=live`. The only blocking item is a test gap. The runtime order disarm is the main runtime barrier, and no test checks that `IBSource.connect` actually calls it. I deleted that call in a mutation probe and all 106 remaining connect, CLI and safety tests still passed. The fix is a few lines of test code. I didn't edit any repo files and didn't connect to any gateway.

Scope: `git diff main..HEAD -- . ':!tests/fixtures' ':!uv.lock'`, plus `pyproject.toml` and the ib_async entry in `uv.lock`. I paid particular attention to fe39755, 127f714 and 55997fe: the future-before-ladder cache (`fut_memo`) and the roll restart.

## SPEC §3 rule by rule

| # | Rule | Status | Evidence |
|---|---|---|---|
| 1 | Live needs three opt-ins | PASS (stricter: spotcheck has no live path at all) | `check_mode` only allows unset or `record`/`paper`/`sim`, after trimming and case-folding (`safety.py:25-32`). It runs in the CLI (`spotcheck/cli.py:106`) and again in `IBSource.connect` (`ibsource.py:107`). There is no `--live` flag: argparse exits with code 2 (`test_live_flag_does_not_exist`). `live.toml` is never read. |
| 2 | Refuse 4001 | PASS | `ALLOWED_PORTS = {4002}` and the check requires a plain int (`safety.py:7,19-22`). It runs in the CLI before `ibsource` is even imported, and in `connect` before `IB()` is built (`ibsource.py:108`). Tested with 4001/7496/7497/4003, `4002.0`, `np.int64` and `True`. |
| 3 | Every order goes through `risk.check` | PASS (no order path exists) | The static grep and AST scan are clean, and the runtime disarm runs before `connectAsync` (`ibsource.py:112`). Nothing imports gateway, risk, quoter or hedger (subprocess test). See finding I1: the runtime disarm is not covered at its call site. |
| 4 | Limits from `config/limits.toml` | N/A (no orders or positions) | `limits.toml` and `live.toml` are untouched: `git diff main..HEAD --name-only` shows no limits, live, .env or secret paths. |
| 5 | Stale-data kill | N/A (no quotes) | Data staleness is flagged instead: the 5 s ladder-to-future gate, `df_flag`, and null treated as flagged. |
| 6 | Event blackout | N/A | No resting quotes. |
| 7 | Kill switch | N/A | No resting orders. On any exit, `_run`'s `finally` disconnects (`spotcheck/cli.py:100-101`), and so does `connect`'s `except BaseException` (`ibsource.py:120-122`). |
| 8 | Secrets / `.env` | PASS (see M6) | `.gitignore:1-2` ignores `.env` and `.env.*`. The only environment variable read is `BMM_MODE`. Grep finds no dotenv, getenv, password, secret or token. |
| 9 | No agent edits to limits/live, no `--live` runs | PASS | As for rules 1 and 4. |

Spotcheck-specific constraints:

| Constraint | Status |
|---|---|
| No place/modify/cancel, static | PASS. Grep is clean; the AST scan covers 16 files and is not vacuous. |
| No place/modify/cancel, runtime | PASS in the code; **the test is missing (I1)**. |
| Ports {4002}; loopback unless `--allow-host`; client id > 0 | PASS. Each guard is checked twice and every check is mutation-tested (table below). |
| Read-only connect | PASS (`readonly=True`, tested) |
| Minimal startup fetch | PASS with caveat M1. `StartupFetch(0)` is passed, but ib_async 2.1.0 still sends `reqPositions` unconditionally. |
| Paper (DU) account check before any data request the spotcheck makes | PASS. `IBSource` is returned only after `check_accounts` (`ibsource.py:119-123`), and every spotcheck request goes through that instance. |
| New request paths (`fut_memo`, roll restart) | PASS. Both only call `reference_future` and `ladder`, which use `reqContractDetailsAsync` and `reqTickersAsync`. Both run after `connect` returns. The restart recursion is at most one level deep (the restarted call has `cached=None`). |
| `SafetyError` never swallowed | PASS. `collect.py:371-372` re-raises it, and `CAUGHT` (`collect.py:44`) does not include it, including on the re-quote and restart path. |
| Exit codes 0 / 2 / 3 | PASS. 0 if at least one product is ok, 2 if all failed or on error, 3 on any `SafetyError`, before or after connect (`spotcheck/cli.py:104-124`). This matches the plan (`docs/plans/spotcheck.md:594`). |
| ib_async `>=2.1,<3` | PASS. `pyproject.toml` has `ib_async>=2.1,<3`; `uv.lock:121` has specifier `>=2.1,<3` and `uv.lock:368-369` resolves `ib-async` 2.1.0; the installed version is 2.1.0. |
| Rule constants pinned | PASS. No constant lines changed in `rule.py` between fe39755 and HEAD, and `test_constants_pinned` is intact. |

## Findings

### Important

**I1. The runtime order disarm is not tested where it's used, so the guard test can pass without it.**
- Where: `src/bmm/spotcheck/ibsource.py:112`, and the connect tests at `tests/spotcheck/test_ibsource.py:137-186`.
- Scenario: someone deletes or moves `disarm(ib)` in `IBSource.connect`, for example below `connectAsync`. All connect and CLI tests still pass, because `test_order_methods_raise_on_fake` and `test_order_methods_raise_on_real_ib_async` call `disarm()` directly, and `FakeIB`'s order methods are silent no-ops. The order methods stay live on the real `IB`, and only the static scan stands in the way. That scan can be evaded (M2).
- Evidence: a scratch plugin replaced `ibsource.disarm` with a no-op. Running the ibsource, CLI and safety tests except `test_order_methods_raise*` gave `106 passed, 9 deselected`.
- Fix (tests only):
  - Make `FakeIB.connectAsync` assert that `object.__getattribute__(self, "placeOrder")` raises `SafetyError`, and the same for `cancelOrder`, `reqGlobalCancel` and `client.placeOrder`. This proves the disarm happens before the socket opens.
  - Or add a test that builds a real `IB()` through `ib_factory`, with `connectAsync` monkeypatched to a coroutine that records whether `ib.placeOrder(None)` raises `SafetyError`.
  - Re-run my mutation probe: it should now fail.

### Minor

**M1. ib_async still requests positions before the DU check, and the code comment says it doesn't.** This is the deferred M8, still unfixed.
- Where: the comment at `ibsource.py:114-115`; the library code is ib_async `ib.py:2056` (`reqs["positions"] = self.reqPositionsAsync()`, which runs regardless of `fetchFields`).
- Scenario: a live login on port 4002 gets one read-only `reqPositions` before `check_accounts` refuses it. No order risk. The comment and `test_connect_skips_startup_fetch` overstate what `StartupFetch(0)` does.
- Evidence: a probe with a stubbed client, `StartupFetch(0)`, `readonly=True` and a `U…` account sent exactly `['client.connectAsync(handshake)', 'reqPositions']`.
- Fix: correct the comment. If the owner wants zero pre-check requests, do the low-level `ib.client.connectAsync(...)` handshake, check `ib.client.getAccounts()`, and only then continue.

**M2. The static scan can be evaded through `__getattribute__`, `operator.methodcaller` and `inspect.getmembers`.**
- Where: `tests/spotcheck/test_safety.py:27-32` and `:122-134`.
- Evidence: the scan flags nothing in any of these:
  - `object.__getattribute__(ib, "place"+"Ord"+"er")`
  - `operator.methodcaller("place"+"Ord"+"er", c, o)`
  - `ib.__getattribute__("cancel"+"Ord"+"er")`
  - `dict(inspect.getmembers(ib))[...]`
- Runtime cover: the disarm covers the named methods on `ib` and `ib.client`. It does not cover `ib.client.send` or `sendMsg`, which can send a raw order message.
- Fix:
  - Add `__getattribute__` to `ORDER_ATTRS`.
  - Add `methodcaller` and `getmembers` to `ORDER_CALLS`, and add each as an `EVASIONS` case.
  - Optionally, wrap `ib.client.send` so that it raises `SafetyError` when the first field is an order message id. The ids are PLACE_ORDER=3, CANCEL_ORDER=4, EXERCISE_OPTIONS=21 and REQ_GLOBAL_CANCEL=58; check them against ib_async's own constants.

**M3. On the roll restart triggered at the re-quote, a failure inside the restart is swallowed as a "re-quote failure".**
- Where: `collect.py:248-263`.
- Scenario: the recursive `collect_once` at `:254` runs inside `try … except CAUGHT`. If the restarted snapshot raises `SourceError` or a timeout, the outer call writes a snapshot with the pre-roll future (`und_px_after=None`, `df_flag=None`) and caches the old future again (`:312-313`). It also logs a misleading message, and the failure isn't counted towards the 3-strike drop.
- Evidence: a probe with a roll at the re-quote and a failing ladder on restart gave snapshot 2 with `con_id 1001, und_px_after None, df_flag None`; snapshot 3 recovered (`con_id 1002`).
- Impact: data quality only. The snapshot lands in the non-simultaneous bucket, and `SafetyError` still propagates.
- Fix: move the roll check and restart out of the `try` block, or re-raise from the restart. Add a test for the re-quote roll path; only the before-quote roll is tested (`test_roll_invalidates_cached_future`).

**M4. On an uncached snapshot, a contract change between the two future quotes is not detected.**
- Where: `collect.py:250` (`... and cached is not None`).
- Scenario: on a first or restarted snapshot, if `fut2.con_id != fut.con_id`, F averages two different contracts without any warning. A probe with a new con_id on every call confirmed it.
- Impact: data quality only. It's rare, because resolution is deterministic for a fixed question date.
- Fix: whenever con_ids differ, set `f_after=None` and log a warning.

**M5. `_mid()` runs before the roll check on the cached path.**
- Where: `collect.py:173-174`.
- Scenario: a rolled future with no price raises before the cache is dropped, so the snapshot fails instead of restarting.
- Fix: check `con_id` before calling `_mid`.

**M6. The REFUSED message prints the full account ids, including a live account number.**
- Where: `safety.py:49-52`.
- These are identifiers, not credentials, but the message goes to stderr and into any captured logs.
- Fix: mask them, for example `U***4567`.

**M7. Hygiene items.**
- `tests/spotcheck/test_rule.py.bak` was committed in 55997fe and should be deleted. pytest doesn't collect it, so it has no effect.
- `disarm`'s regex also replaces the `ib.cancelOrderEvent` event with a stub (`ibsource.py:43-46`). This is harmless: the event is only emitted from inside `cancelOrder` (ib `ib.py:848`), which is itself stubbed. Consider adding `(?!event)` to the pattern or excluding non-callables.

## Evidence

1. **Order-surface grep:**
   `grep -rnE "placeOrder|cancelOrder|reqGlobalCancel|whatIfOrder|bracketOrder|oneCancelsAll|exerciseOptions|Order\s*\(|MarketOrder|LimitOrder|StopOrder" src/bmm/spotcheck src/bmm/cli.py` → no matches (rc=1).
2. **ib_async imports and calls:**
   - The only import is `ibsource.py:19` (`IB, Contract, Future, FuturesOption, StartupFetch`).
   - The only IB calls are `connectAsync`, `managedAccounts`, `disconnect`, `reqContractDetailsAsync`, `reqTickersAsync`, `reqSecDefOptParamsAsync` and `qualifyContractsAsync` (`ibsource.py:116-305`).
   - `reqTickersAsync` defaults to `regulatorySnapshot=False` (ib `ib.py:2183,2195`).
3. **Live, ports and secrets grep:**
   `grep -rnE "\-\-live|4001|7496|7497|dotenv|\.env\b|getenv|environ|password|secret|token|limits\.toml|live\.toml"` → only docstrings and comments, plus `check_mode(os.environ)` at `spotcheck/cli.py:106` and `ibsource.py:107`.
4. **Sensitive paths:** `git diff main..HEAD --name-only | grep -iE "limits|live\.toml|\.env|secret"` → nothing (rc=1). The only config change is the new `config/spotcheck.example.toml`, which sets no ports or modes.
5. **Disarm on a real `IB()` (never connected):**
   - Disarmed: `ib.{bracketOrder, cancelOrder, cancelOrderEvent, exerciseOptions, oneCancelsAll, placeOrder, reqGlobalCancel, whatIfOrder, whatIfOrderAsync}` and `client.{cancelOrder, exerciseOptions, placeOrder, reqGlobalCancel}`.
   - Every stub raises `SafetyError`.
   - Remaining order-related names are read-only (`reqOpenOrders`, `reqCompletedOrders`, `reqAutoOpenOrders`), except `client.send` and `sendMsg` (see M2).
6. **Startup probe** (M1): with `StartupFetch(0)`, the only requests sent before `check_accounts` are the handshake and `reqPositions`.
7. **Tests:**
   - `uv run pytest -q tests/spotcheck/test_safety.py tests/spotcheck/test_ibsource.py tests/test_cli.py` → `115 passed in 2.18s`.
   - `uv run pytest -q tests/spotcheck/test_collect.py -k "roll or before_ladder or gate or requote or safety_error or isolated"` → `9 passed`.
8. **Guard mutation probes** (scratch pytest plugins that replace one guard with a no-op; repo untouched):

   | Mutation | Result |
   |---|---|
   | `ibsource.check_accounts` | 1 failed |
   | `ibsource.check_mode` | 1 failed |
   | `ibsource.check_client_id` | 5 failed |
   | `ibsource.check_host` | 1 failed |
   | `ibsource.check_port` | 4 failed |
   | `cli.check_port`, `cli.check_mode`, `cli.check_host`, `cli.check_client_id` | 3 failed each |
   | `ibsource.disarm` | **0 failed (I1)** |

9. **Scanner mutation:**
   - The scan covers 16 files and finds nothing in the repo.
   - An injected `ib.client.reqAutoOpenOrders(True)` is flagged.
   - The four evasions in M2 are not flagged.
10. **Roll-path probe:**
    - Roll seen at the re-quote: the snapshot restarts, 97 rows / 97 unique snapshot ids, call order `future, ladder, expiries, future → ladder, expiries, future`.
    - Roll plus a failing restart: the failure is swallowed (M3).
    - A new con_id on every call: the recursion stays one level deep and contracts are mixed silently (M4).
11. **ib_async pin:** `pyproject.toml` has `"ib_async>=2.1,<3"`; `uv.lock:121` has `specifier = ">=2.1,<3"` and `uv.lock:368-369` resolves `name = "ib-async"`, `version = "2.1.0"`; the installed version is 2.1.0.
12. **Rule constants:** `git diff fe39755..HEAD -- src/bmm/spotcheck/rule.py | grep -E "^[-+][A-Z_]+ *[:=]"` → no output.

To get APPROVE: fix I1 (a test that fails if `connect` stops disarming). I recommend M2 and M3 in the same PR; the rest can be deferred.
