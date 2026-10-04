# CLAUDE.md — binary-mm

Market-making system for event-contract binaries priced as digital options.
**Spec index:** `SPEC.md`, which links one file per area in `docs/spec/`. Venue facts: `research/venue_facts.md`. Workflow: `docs/claude-code-workflow.md`. Domain notes: `docs/domain_notes.md`.

## Process tiers (state the tier at the start of every task)
Default to the **lowest** tier that applies.
- **A — order path** (`risk/`, gateway order methods, `quoter/`, `hedger/`): TDD, opus reviewers (risk-auditor + quant-reviewer), full safety tests.
- **B — read-only tools** (recorders, collectors, scouts, reports): sonnet implementer; ONE quant-reviewer pass at the end, on math-touching diffs only. Safety = one no-order-calls test + one port/env test. No adversarial or look-alike test sprawl.
- **C — one-off analysis, scripts, config/docs edits:** main session only, no subagents, minimal tests.

## Token hygiene
- Test output: `uv run pytest -q -x 2>&1 | tail -20`. Never print full passing output.
- Never `cat` parquet, logs, or files > 200 lines; use head/grep/wc or a summary script.
- Reviewers receive `git diff` plus the single spec file the change implements, not the repo.
- If a spec exists, skip brainstorming and go straight to writing-plans.
- Plans: <= 8 tasks; batch small tasks; no task under ~20 min of work.
- Turn caps: Tier B goals <= 25 turns, Tier C <= 10.

## Non-negotiables (full text: `docs/spec/00-safety.md`)
- Never run anything with `--live`, never set `BMM_MODE=live`, never connect to IB Gateway port 4001.
- Never edit `config/limits.toml` or `config/live.toml`. Propose changes in a PR description instead.
- Every broker order goes through `risk.check`. If you find a path that doesn't, stop and report it.
- Never read, print or commit `.env` or credentials.
- If a venue fact is uncertain, mark it `[?]` in `research/venue_facts.md`. **Do not guess** contract specs, fees or settlement rules.

## Commands
- Setup: `uv sync --all-extras`
- Check everything: `make check` (ruff + mypy + pytest)
- Tests only: `uv run pytest -q -x 2>&1 | tail -20`
- Recorder (market data only, safe): `uv run bmm record --config config/record.toml`
- Replay/sim: `uv run bmm sim --from YYYY-MM-DD --to YYYY-MM-DD`
- Spot check (market data only, paper port 4002): `uv run bmm spotcheck collect|report`

## Conventions
- Python 3.12, asyncio, `ib_async` for IBKR. Type hints everywhere; mypy strict on `pricing/`, `risk/`, `quoter/`.
- Time: all timestamps are int ns UTC from `bmm.clock`. Never call `datetime.now()` directly.
- Prices are floats in [0,1] for binaries; round only at the order boundary via the `quoting` tick helpers.
- ForecastEx is BUY-ONLY: YES=Call, NO=Put; offering YES at `a` == bidding NO at `1-a`. Use `to_forecastex_orders`.
- Money math in tests: use `pytest.approx` with explicit tolerances.
- Small PRs. One task per branch or worktree.

## Definition of done
1. Tests written first and passing (`make check` green).
2. Reviews per tier: A = risk-auditor + quant-reviewer; B = one quant-reviewer pass if the diff touches math; C = none.
3. Evidence in the transcript: the command output that proves the acceptance criterion.
