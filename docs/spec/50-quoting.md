# Spec 50: Paper/shadow quoting and live forward test

> Part of the binary-mm spec. Index: [`SPEC.md`](../../SPEC.md). Section numbers are the original SPEC numbering.

### Phase 5 — Paper quoting (or "shadow quoting" if no paper support)
- Quoter plus risk plus hedger end to end against paper. If ForecastEx has no paper support: shadow mode, where we compute and log the orders we would send and score them against the live book with the Phase 4 fill model.
- **Accept:** 5 sessions, zero risk-rule violations, all kill paths tested live (stale feed, blackout, daily stop, kill file); shadow/paper results within the sim's confidence band.

### Phase 6 — Live forward test (owner-initiated only)
- Owner flips live flags. 1–10 lot sizes. Daily attribution report.
- **Owner decision gate after 6 weeks:** scale / restructure / kill per the criteria in `docs/strategy_notes.md`.
