# Spec 10: Venue verification, data model, recorder, infra

> Part of the binary-mm spec. Index: [`SPEC.md`](../../SPEC.md). Section numbers are the original SPEC numbering.

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

## 9. Infra and costs (target ≤ $150/mo)
- VPS: one small Linux instance in a US-East region (2 vCPU / 4 GB is plenty) running IB Gateway (headless, with IBC for auto-restart/2FA handling), the recorder and the quoter. Systemd units. Daily parquet sync to cheap object storage.
- Data: IBKR market data subscriptions (CME futures + options needed for the shortlist; OPRA only if SPX options are used). **Confirm non-professional eligibility.**
- Optional later: Databento CME live, only if Phase 3/5 markouts show we lose to faster quoters because of IBKR's sampled data.
- Monitoring: structured logs (structlog JSON); a heartbeat file plus a cron alert (email/Telegram) if no heartbeat for 60s.
