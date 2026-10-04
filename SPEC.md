# SPEC: binary-mm — market making event-contract binaries as digital options

Owner: Reagan · Spec version 0.1 · 2026-10-03
Read with: `CLAUDE.md` (working rules), `research/venue_facts.md` (verified facts and open questions), `docs/claude-code-workflow.md` (how agents run this).

---

## 1. Objective

Build a small, robust system that:
1. **Records** event-contract order books (ForecastEx first), the reference futures, and the relevant options chains.
2. **Prices** each binary as a digital option off the options surface (model-free call spreads anchored, delta/gamma between anchor updates).
3. **Measures** whether a quoting edge exists (spread capture + incentive pool share − adverse selection), *before* risking capital.
4. **Quotes** in small lots under hard risk limits, **hedges** delta with micro futures, and **attributes** P&L daily.

Capital: $10k. Fixed-cost budget: ≤ $150/month all-in. Success is measured by **markout-adjusted edge**, not gross P&L.

## 2. Non-goals (v1)

- No sub-millisecond engineering. The latency floor is the IBKR order path. Do not optimise below it.
- No crypto venues, Pascal, HIP-4 or Kalshi trading in v1. A Kalshi **read-only** feed for cross-checks is allowed.
- No ML. No parameter search beyond the simple grid in Phase 4.
- No UI beyond CLI reports and static HTML/markdown dashboards.

## 4. Architecture

```
src/bmm/
  config.py          pydantic settings: modes, limits, venue params, calendars
  clock.py           single monotonic+UTC clock; all timestamps ns UTC
  gateway/ibkr.py    ib_async wrapper: connect, contract discovery, mktdata, orders (risk-gated)
  feeds/futures.py   ES/MES/NQ/GC top-of-book via IBKR (pluggable for Databento later)
  feeds/options.py   options chain snapshots (ES/EW options, SPX, GC/OG as needed)
  feeds/forecastex.py  ForecastEx book/trades via IBKR (YES=C, NO=P mapping)
  feeds/kalshi.py    READ-ONLY cross-check feed (optional)
  recorder/          append-only parquet writer, schema registry, gap detection, daily rollover
  pricing/           digital.py, quoting.py, fees.py   <-- EXISTS, tested
  surface/           anchor builder: call-spread digitals per strike + SVI sanity fit
  quoter/            fair -> quotes -> venue orders (ForecastEx buy-only translation)
  risk/              limits, stale-data, blackout, kill switch, daily stop
  hedger/            net delta aggregation -> MES/MNQ/MGC orders with deadband
  sim/               event-driven replay of recorded data + conservative fill model
  analytics/         microstructure stats, markouts, attribution, reports
  cli.py             `bmm record|price|sim|quote|kill|report`
```

Process model v1: a single asyncio process per mode. The recorder can run standalone 24/5. Quoter and hedger share one process with the risk module in-process (no IPC for the order path).

## 7. Phases, deliverables, acceptance criteria

Each phase ends with a `/goal` condition (see `docs/claude-code-workflow.md`). Phases 1–3 can run in parallel worktrees once Phase 0 is done.

| Spec file | Covers |
|---|---|
| [docs/spec/00-safety.md](docs/spec/00-safety.md) | §3 hard safety rules, §8 testing standards |
| [docs/spec/10-recorder.md](docs/spec/10-recorder.md) | §5 data model, Phase 0 venue verification, Phase 1 recorder, §9 infra |
| [docs/spec/20-pricing.md](docs/spec/20-pricing.md) | §6 pricing methodology, Phase 2 fair value |
| [docs/spec/30-analytics.md](docs/spec/30-analytics.md) | Phase 3 microstructure analytics |
| [docs/spec/40-sim.md](docs/spec/40-sim.md) | Phase 4 simulator |
| [docs/spec/50-quoting.md](docs/spec/50-quoting.md) | Phase 5 paper/shadow quoting, Phase 6 live forward test |

One-off: [docs/spotcheck_spec.md](docs/spotcheck_spec.md) (venue mispricing spot check).

## 10. Open questions for the owner (agents: do not decide these)
1. Final product list after Phase 3.
2. Limit values in `config/limits.toml`.
3. Whether to apply for the retainer program and when.
4. Whether to add a Databento feed.
