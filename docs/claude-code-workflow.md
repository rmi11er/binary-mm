# Claude Code workflow for binary-mm

## One-time setup

```bash
# in Claude Code
/plugin install superpowers@claude-plugins-official
#   (or: /plugin marketplace add obra/superpowers-marketplace
#        /plugin install superpowers@superpowers-marketplace)
```
Then `uv sync --all-extras`, copy `config/*.example.toml` → real files, and set `config/limits.toml` yourself.
Restart Claude Code once so the project subagents in `.claude/agents/` are picked up.

## Model routing

| Work | Model | Why |
|---|---|---|
| Main session: brainstorming, plans, pricing/risk design, debugging weird P&L | **Opus 5.5** (`/model opus`) | Judgment-heavy, low volume |
| `quant-reviewer`, `risk-auditor` subagents | **Opus** (pinned in frontmatter) | Reviews are where errors are expensive |
| `implementer`, `test-writer`, `venue-researcher`, `data-validator` | **Sonnet 5.5** (pinned) | Well-specified, high volume, cheaper |
| `/goal` evaluator | Haiku (default small fast model) | Only reads the transcript; leave it alone |

You can pin full IDs instead of aliases (`model: claude-opus-5-5` / `claude-sonnet-5-5`) if you want versions locked. Don't override `ANTHROPIC_DEFAULT_HAIKU_MODEL` just for /goal: it also changes background tasks such as summarization.

## The loop per phase

1. `/kickoff-phase N`: superpowers **brainstorming** then **writing-plans**, which produces `docs/plans/phase-N.md`. You approve the plan. This is the step where your discretionary input matters most; don't rubber-stamp it.
2. Execute with superpowers **subagent-driven-development**: a fresh `implementer` per task, with the plugin's two-stage review (spec compliance, then code quality). Use **using-git-worktrees** to run Phases 1/2/3 in parallel after Phase 0.
3. Wrap the execution in a `/goal` (below) so it runs to a verifiable end state without you prompting each turn.
4. `/review-order-path` before merge: risk-auditor and quant-reviewer in parallel.
5. superpowers **verification-before-completion** plus **finishing-a-development-branch** to merge.

## /goal rules of thumb (from the docs)

- The evaluator **does not run commands or read files**. It judges only what's in the transcript. So every condition says *which command proves it* and asks Claude to print the output.
- One measurable end state, a stated check, constraints, and a turn bound. Conditions can be up to 4,000 characters.
- Run in **auto mode** for unattended turns, *except* sessions touching `gateway/` or `risk/`, which stay in manual mode so you see every tool call.
- If a subagent or background command is still running, evaluation is deferred to the next turn.
- `/goal` alone shows status; `/goal clear` stops it.

## Ready-to-paste goal conditions

**Phase 0 (venue research)**
```
/goal Every [?] item in the ForecastEx table of research/venue_facts.md is resolved with a [V] or [S] tag and source URL, or explicitly marked "needs owner" with what was searched; research/product_shortlist.md exists with >= 2 candidate products, each listing reference price/time, strike grid, incentive eligibility and a go/no-go reason. Prove it by printing `grep -c "\[?\]" research/venue_facts.md` (ForecastEx section must be 0) and the shortlist file. Use the venue-researcher subagent. Do not modify any file outside research/. Stop after 30 turns.
```

**Phase 1 (recorder)**
```
/goal All tasks in docs/plans/phase-1.md are checked off; `make check` exits 0 with output shown; `uv run pytest -q --cov=src/bmm/recorder` shows >= 90% coverage; a recorder dry-run against recorded fixtures produces all SPEC section 5 tables and a recorder_health.md, shown via `ls -R data/` and `cat` of the health report. No file under config/ other than *.example.toml is modified, no code connects to port 4001, and risk-auditor has returned APPROVE on src/bmm/gateway. Stop after 60 turns.
```
(The 6-hour live-market-data soak is run by you on the VPS, not inside the goal.)

**Phase 2 (fair value)**
```
/goal docs/plans/phase-2.md tasks are complete; `make check` exits 0 (output shown); `uv run bmm price --replay <dates>` produces the fair table for every shortlisted contract on 3 recorded days with zero NaN rows (print the row and NaN counts); reports/anchor_vs_svi.md exists; quant-reviewer returns PASS or PASS-WITH-NITS with nits addressed. Stop after 50 turns.
```

**Phase 3 (go/no-go analytics)**
```
/goal reports/phase3_<date>.md exists and contains, for each shortlisted product: spread/depth by moneyness and time-to-resolution, trade frequency/size, competitor reaction latency, trade markouts at 1s/10s/60s/300s vs our fair, mid-vs-fair bias by moneyness, incentive pool share estimate, and a ranked recommendation with EV/day and confidence bands; data-validator reported PASS or WARN (not FAIL) on the input range; quant-reviewer returned PASS. Print the report headings and the validator verdict. Stop after 40 turns.
```

**Phase 4 (sim)**
```
/goal `uv run bmm sim` runs on two disjoint recorded date ranges and writes reports/sim_<range>.md decomposing P&L into spread, incentives, markout cost and hedge cost; the fill model is trade-through-only (show the unit test proving a print AT our price does not fill); the report states whether positive P&L depends on incentives alone; quant-reviewer PASS. Stop after 50 turns.
```

Phase 5+ (paper/shadow and live): **no /goal.** Run them interactively. These are the phases where an autonomous loop is the wrong tool.

## Handy prompts

- "Dispatch venue-researcher and data-validator in parallel: researcher resolves [?] items 3–5; validator checks data/2026-10-0*."
- "Use systematic-debugging: fair value jumps 3¢ at re-anchor on 2026-10-14 10:02 ET. Find the root cause before changing code."
- "Before you implement, write the discretionary thesis for this parameter change into docs/strategy_notes.md."
