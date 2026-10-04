# Spec 40: Simulator and strategy parameters

> Part of the binary-mm spec. Index: [`SPEC.md`](../../SPEC.md). Section numbers are the original SPEC numbering.

### Phase 4 — Simulator + strategy parameters
- Event-driven replay. **Conservative fill model:** fill only if a trade prints *through* our price (not at it), or the book crosses it. Queue position unknown → assume back of queue.
- Strategy params: half_spread, skew_per_contract, re-anchor thresholds, pull-quote windows. Small grid only.
- Incentive model: estimate our score share per period from the simulated resting book.
- **Accept:** sim P&L decomposed into spread / incentives / markout cost / hedge cost; results stable across ≥ 2 disjoint date ranges; report flags if positive P&L depends on incentives alone.
