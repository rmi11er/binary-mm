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

## 3. Hard safety rules (non-negotiable — enforced in code and in review)

1. **Live trading is opt-in at three layers:** `BMM_MODE=live` env var, `--live` CLI flag, and `config/live.toml` containing `i_understand_this_trades_real_money = true`. Missing any of them → paper/sim only. Default is `record`.
2. IB Gateway ports: paper 4002, live 4001. Code must refuse to connect to 4001 unless rule 1 holds.
3. **Every order passes `risk.check(order, state)`.** There is no code path to the broker that bypasses it. A test asserts this (grep plus unit test on the gateway wrapper).
4. **Limits in v1 config (set by owner, not by agents):**
   - max contracts per order: 10 (forward-test lots)
   - max net YES exposure per strike: 100 contracts
   - max gross collateral at risk: $2,000
   - max |net digital gamma| inside the final 60 min before resolution: config value; quoting **stops** at T−15 min by default
   - daily loss stop: $150 → cancel all, flatten delta, halt until manual restart
5. **Stale-data kill:** if the reference futures feed has no update for > 3s during RTH, or options anchors are older than the configured max age → cancel all quotes.
6. **Event blackout:** no resting quotes from T−60s to T+120s around scheduled releases (CPI, NFP, FOMC, etc., from a calendar file), or around the cash open and close if configured.
7. Global kill switch: `bmm kill` CLI command, plus a file-based flag (`/var/run/bmm.kill`) checked every loop.
8. Secrets only from env or `.env` (gitignored). Never log credentials. Never commit `.env`.
9. Agents **never** change values in `config/limits.toml` or `config/live.toml`, and never run anything with `--live`. Only the owner does.

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

## 5. Data model (parquet, partitioned by `date=YYYY-MM-DD/source=...`)

All tables carry `ts_recv_ns` (local receive), `ts_exch_ns` (if provided), `seq` (local monotonic).

| Table | Key fields |
|---|---|
| `fx_book` | contract_id, right (C/P), strike, expiry_ts, level, bid_px, bid_sz, ask_px, ask_sz |
| `fx_trades` | contract_id, right, px, sz, aggressor (if derivable) |
| `fut_tob` | symbol, bid, ask, bid_sz, ask_sz, last |
| `opt_snap` | underlying, expiry, strike, right, bid, ask, iv_bid, iv_ask (if provided), und_px |
| `fair` | contract_id, fair, fair_flat, anchor_age_ms, F, sig_K, delta, gamma |
| `orders` / `fills` | order_id, intent, px, sz, state transitions; fill px/sz/fees/rebates |
| `incentives` | product, period, pool_usd, our_score, total_score (from daily report) |

Gap detection: per source, flag any interval with no messages > threshold during open hours. Write `gaps` table. Daily `recorder_health.md` report.

## 6. Pricing methodology (implemented core in `src/bmm/pricing`)

6.1 **Anchor** (each options update): for strike K, pick listed options strikes K_lo ≤ K ≤ K_hi with the tightest liquid spread. Digital = (C(K_lo) − C(K_hi)) / (K_hi − K_lo) using mids, with a liquidity-weighted mid if the spread is wide. If K is off-grid, interpolate digitals across neighbouring spreads. Also fit SVI per expiry as a sanity check. Flag anchors where model-free and SVI disagree by > 1¢.

6.2 **Fast update** (each futures tick): fair(t) = anchor + Δ·(F_t − F_anchor) + ½Γ·(F_t − F_anchor)², using `digital_delta/gamma` at the anchor vol. Re-anchor when |F_t − F_anchor| exceeds a threshold or anchor age exceeds the max.

6.3 **Time**: T = resolution timestamp − now, in years. The options expiry rarely matches the contract's reference time, so interpolate total variance across expiries. TODO v1.1: trading-time clock (overnight variance weight, event-day variance bumps).

6.4 **Underlying basis**: if the contract references a futures price, F = that futures contract (no carry). If it references a cash index, F = futures − fair basis. Phase 0 determines which.

6.5 **Skew matters**: with equity skew, the upside digital trades above flat-vol N(d2) by roughly 1–2¢ near ATM for 1DTE (see test `test_smile_digital_matches_call_spread_on_skewed_smile`). That is the same size as the spread, which is the core thesis.

## 7. Phases, deliverables, acceptance criteria

Each phase ends with a `/goal` condition (see `docs/claude-code-workflow.md`). Phases 1–3 can run in parallel worktrees once Phase 0 is done.

### Phase 0 — Venue verification (research, no code)
- Resolve every **[?]** in `research/venue_facts.md` using official docs and CFTC filings. Write results back into that file with source URLs.
- Produce `research/product_shortlist.md`: candidate products with reference price/time, typical strikes, incentive eligibility, pool size if published, and a go/no-go reason for each.
- **Accept:** zero [?] items remain in the ForecastEx table (resolved, or explicitly marked "needs owner — reason"); shortlist has ≥ 2 candidates with sources.

### Phase 1 — Repo foundation + recorder
- uv project, ruff/mypy/pytest in CI (GitHub Actions or local `make check`).
- `gateway/ibkr.py` (paper port only), contract discovery for ForecastEx products on the shortlist, futures and options subscriptions.
- Recorder writing all Section 5 tables, gap detection, daily rollover, `recorder_health.md`.
- Latency probe: place/cancel far-from-market 1-lot limit orders in **paper** and record ack latency distribution (skip if ForecastEx is not in paper; document it).
- **Accept:** `make check` passes; recorder runs 6 continuous hours against paper/live **market data** with zero unhandled exceptions; gaps table populated; a health report is generated; ≥ 90% line coverage on recorder schema/writer code.

### Phase 2 — Surface + fair value service
- `surface/` anchor builder from `opt_snap`, SVI sanity fit, fair-value stream to the `fair` table.
- Replay mode: compute fair values over recorded data.
- **Accept:** on ≥ 3 recorded days, fair-value series produced for every shortlisted contract; anchor-vs-SVI disagreement report; unit tests cover off-grid interpolation, stale anchors and expiry edge cases (T→0 must not produce NaN; quoting must already be off by then).

### Phase 3 — Microstructure analytics (the go/no-go)
From ≥ 10 recorded sessions, report per product:
- spread and depth by strike moneyness and time-to-resolution
- trade frequency and size distribution
- **competitor reaction latency:** time from a futures move of ≥ x points to resting-quote updates
- **trade markouts** vs our fair at +1s/+10s/+60s/+300s (are takers informed?)
- **mid vs fair bias** by moneyness (where is retail mispricing? wings? skew ignored?)
- incentive pool $ vs estimated competing depth → plausible pool share
- **Accept:** `reports/phase3_<date>.md` with all metrics, charts as PNG, and an explicit ranked recommendation with EV/day estimates and confidence bands.

### Phase 4 — Simulator + strategy parameters
- Event-driven replay. **Conservative fill model:** fill only if a trade prints *through* our price (not at it), or the book crosses it. Queue position unknown → assume back of queue.
- Strategy params: half_spread, skew_per_contract, re-anchor thresholds, pull-quote windows. Small grid only.
- Incentive model: estimate our score share per period from the simulated resting book.
- **Accept:** sim P&L decomposed into spread / incentives / markout cost / hedge cost; results stable across ≥ 2 disjoint date ranges; report flags if positive P&L depends on incentives alone.

### Phase 5 — Paper quoting (or "shadow quoting" if no paper support)
- Quoter plus risk plus hedger end to end against paper. If ForecastEx has no paper support: shadow mode, where we compute and log the orders we would send and score them against the live book with the Phase 4 fill model.
- **Accept:** 5 sessions, zero risk-rule violations, all kill paths tested live (stale feed, blackout, daily stop, kill file); shadow/paper results within the sim's confidence band.

### Phase 6 — Live forward test (owner-initiated only)
- Owner flips live flags. 1–10 lot sizes. Daily attribution report.
- **Owner decision gate after 6 weeks:** scale / restructure / kill per the criteria in `docs/strategy_notes.md`.

## 8. Testing standards
- TDD for pricing, risk and quoter (superpowers `test-driven-development`).
- Property tests (hypothesis): quotes always inside [0.01, 0.99], bid < ask, ForecastEx translation consistent (YES ask a ⇔ NO bid 1−a), risk.check never approves beyond limits.
- Golden-file tests for recorder schemas.
- Any change touching `risk/`, `gateway/` order methods, or `quoter/` requires **risk-auditor** subagent sign-off recorded in the PR description.

## 9. Infra and costs (target ≤ $150/mo)
- VPS: one small Linux instance in a US-East region (2 vCPU / 4 GB is plenty) running IB Gateway (headless, with IBC for auto-restart/2FA handling), the recorder and the quoter. Systemd units. Daily parquet sync to cheap object storage.
- Data: IBKR market data subscriptions (CME futures + options needed for the shortlist; OPRA only if SPX options are used). **Confirm non-professional eligibility.**
- Optional later: Databento CME live, only if Phase 3/5 markouts show we lose to faster quoters because of IBKR's sampled data.
- Monitoring: structured logs (structlog JSON); a heartbeat file plus a cron alert (email/Telegram) if no heartbeat for 60s.

## 10. Open questions for the owner (agents: do not decide these)
1. Final product list after Phase 3.
2. Limit values in `config/limits.toml`.
3. Whether to apply for the retainer program and when.
4. Whether to add a Databento feed.
