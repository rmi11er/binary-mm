# CLAUDE.md — binary-mm

You are building a market-making system for event-contract binaries priced as digital options.
**Source of truth:** `SPEC.md`. Venue facts: `research/venue_facts.md`. Workflow: `docs/claude-code-workflow.md`.

## Non-negotiables (read SPEC section 3 in full)
- Never run anything with `--live`, never set `BMM_MODE=live`, never connect to IB Gateway port 4001.
- Never edit `config/limits.toml` or `config/live.toml`. Propose changes in a PR description instead.
- Every broker order goes through `risk.check`. If you find a path that doesn't, stop and report it.
- Never read, print or commit `.env` or credentials.
- If a venue fact is uncertain, mark it `[?]` in `research/venue_facts.md`. **Do not guess** contract specs, fees or settlement rules.

## Commands
- Setup: `uv sync --all-extras`
- Check everything: `make check` (ruff + mypy + pytest)
- Tests only: `uv run pytest -q`
- Recorder (market data only, safe): `uv run bmm record --config config/record.toml`
- Replay/sim: `uv run bmm sim --from YYYY-MM-DD --to YYYY-MM-DD`

## Conventions
- Python 3.12, asyncio, `ib_async` for IBKR. Type hints everywhere; mypy strict on `pricing/`, `risk/`, `quoter/`.
- Time: all timestamps are int ns UTC from `bmm.clock`. Never call `datetime.now()` directly.
- Prices are floats in [0,1] for binaries; round only at the order boundary via the `quoting` tick helpers.
- ForecastEx is BUY-ONLY: YES=Call, NO=Put; offering YES at `a` == bidding NO at `1-a`. Use `to_forecastex_orders`.
- Money math in tests: use `pytest.approx` with explicit tolerances.
- Small PRs. One phase task per branch or worktree.

## Definition of done (every task)
1. Tests written first and passing (`make check` green).
2. For `risk/`, `gateway/`, `quoter/`, `hedger/` changes: risk-auditor subagent review attached.
3. For `pricing/`, `surface/`, `analytics/` math: quant-reviewer subagent review attached.
4. Evidence in the transcript: the test or command output that proves the acceptance criterion.

## Domain notes
- Digital = −dC/dK. Prefer model-free call-spread anchors; Black-76 delta/gamma only for between-anchor updates.
- Negative equity skew makes upside digitals worth more than flat-vol N(d2), by about 1–2¢ near ATM on 1DTE. Do not "simplify" skew away.
- Gamma explodes near the strike as T→0: quoting must stop before resolution (default T−15min).
- Incentive pools are pro rata and finite. Report incentive P&L separately and never let it mask negative markouts.
