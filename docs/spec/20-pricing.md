# Spec 20: Pricing methodology and fair-value service

> Part of the binary-mm spec. Index: [`SPEC.md`](../../SPEC.md). Section numbers are the original SPEC numbering.

## 6. Pricing methodology (implemented core in `src/bmm/pricing`)

6.1 **Anchor** (each options update): for strike K, pick listed options strikes K_lo ≤ K ≤ K_hi with the tightest liquid spread. Digital = (C(K_lo) − C(K_hi)) / (K_hi − K_lo) using mids, with a liquidity-weighted mid if the spread is wide. If K is off-grid, interpolate digitals across neighbouring spreads. Also fit SVI per expiry as a sanity check. Flag anchors where model-free and SVI disagree by > 1¢.

6.2 **Fast update** (each futures tick): fair(t) = anchor + Δ·(F_t − F_anchor) + ½Γ·(F_t − F_anchor)², using `digital_delta/gamma` at the anchor vol. Re-anchor when |F_t − F_anchor| exceeds a threshold or anchor age exceeds the max.

6.3 **Time**: T = resolution timestamp − now, in years. The options expiry rarely matches the contract's reference time, so interpolate total variance across expiries. TODO v1.1: trading-time clock (overnight variance weight, event-day variance bumps).

6.4 **Underlying basis**: if the contract references a futures price, F = that futures contract (no carry). If it references a cash index, F = futures − fair basis. Phase 0 determines which.

6.5 **Skew matters**: with equity skew, the upside digital trades above flat-vol N(d2) by roughly 1–2¢ near ATM for 1DTE (see test `test_smile_digital_matches_call_spread_on_skewed_smile`). That is the same size as the spread, which is the core thesis.

### Phase 2 — Surface + fair value service
- `surface/` anchor builder from `opt_snap`, SVI sanity fit, fair-value stream to the `fair` table.
- Replay mode: compute fair values over recorded data.
- **Accept:** on ≥ 3 recorded days, fair-value series produced for every shortlisted contract; anchor-vs-SVI disagreement report; unit tests cover off-grid interpolation, stale anchors and expiry edge cases (T→0 must not produce NaN; quoting must already be off by then).
