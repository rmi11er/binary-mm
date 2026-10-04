# binary-mm

Market making event-contract binaries (ForecastEx first) priced as digital options off the options surface.

Start here:
1. `SPEC.md`: what to build, the phases, and the hard safety rules
2. `CLAUDE.md`: rules Claude Code follows in this repo
3. `docs/claude-code-workflow.md`: superpowers + /goal + subagents playbook, with paste-ready goal conditions
4. `research/venue_facts.md`: verified facts and open questions (Phase 0 resolves the open ones)

Already built and tested: `src/bmm/pricing` (digital pricing incl. skew, Greeks, call-spread anchors,
total-variance interpolation, hedge sizing, quote construction, ForecastEx buy-only translation, fee schedules).

```bash
uv sync --all-extras && make check
```
