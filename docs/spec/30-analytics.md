# Spec 30: Microstructure analytics (go/no-go)

> Part of the binary-mm spec. Index: [`SPEC.md`](../../SPEC.md). Section numbers are the original SPEC numbering.

### Phase 3 — Microstructure analytics (the go/no-go)
From ≥ 10 recorded sessions, report per product:
- spread and depth by strike moneyness and time-to-resolution
- trade frequency and size distribution
- **competitor reaction latency:** time from a futures move of ≥ x points to resting-quote updates
- **trade markouts** vs our fair at +1s/+10s/+60s/+300s (are takers informed?)
- **mid vs fair bias** by moneyness (where is retail mispricing? wings? skew ignored?)
- incentive pool $ vs estimated competing depth → plausible pool share
- **Accept:** `reports/phase3_<date>.md` with all metrics, charts as PNG, and an explicit ranked recommendation with EV/day estimates and confidence bands.
