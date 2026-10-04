# Spot-check spec: are venue binaries mispriced vs the options surface by more than fees?

One-off, outside the phase rollout. **Market data only: no order code may be imported or called.**
Goal: a decisive go/kill on (a) ForecastEx S&P/NDX daily contracts and (b) CME gold event contracts, from 2–3 sessions of snapshots.

## Already built (do not rewrite)
`src/bmm/spotcheck/analyze.py` (tested in `tests/test_spotcheck.py`):
- `fair_band(chain, K)`: model-free digital fair (interpolated call-spread slopes) **plus replication-grade bounds** from option bid/ask (D is monotone in K, so adjacent-interval averages bound it).
- `edges(ladder, chain, fee_per_side)`: per-strike mid gap, taker edges (vs fair) and model-free arb edges (vs bounds), after fees.
- `capturable_dollars(rows, threshold, model_free)`: $ edge takeable at displayed size.

## To build
`src/bmm/spotcheck/collect.py` plus CLI `bmm spotcheck collect|report`.

### Collect (ib_async, IB Gateway **paper port 4002 only**, market data only)
Per snapshot, for each target product:
1. **Venue ladder** for the current question date: every strike's YES and NO top of book with sizes.
   - ForecastEx: `secType=OPT`, `exchange=FORECASTX`. YES=C, NO=P, buy-only. Normalise: `yes_bid` = best YES bid, `yes_ask` = 1 − best NO bid. Sizes accordingly. Log raw YES/NO quotes too, so the normalisation can be audited.
   - CME gold event contracts: `secType=FOP`, trading class `EC…` (discover via contract details). $100 payout, $1 tick → divide prices by 100. Both sides tradable, so use the displayed bid/ask directly.
2. **Reference futures** top of book: the exact contract each binary references (ES/NQ for ForecastEx, GC for gold; month per the contract rules).
3. **Options chain** on the same futures, at the expiry whose settlement best matches the binary's fixing time: call bid/ask for all strikes within ±4σ of F. E-mini options for ES/NQ; gold options (OG family) for GC. If no expiry matches the fixing time exactly, record the nearest bracketing expiries and flag `expiry_mismatch=true` (fair is approximate; bounds still valid only if the expiry matches).
4. Timestamps in ns UTC for every element; snapshot id.

Cadence: every 5 min across the venue's trading hours. Every 1 min from 2:30–3:00 PM CT, and from 3:00–3:30 PM CT to observe **whether the venue book stays open after the CME settlement fixes the outcome** (log any resting quotes and trades in that window).

Write parquet: `data/spotcheck/date=…/{ladder,futures,options}.parquet`.

### Report (`reports/spotcheck_<dates>.md` + PNGs)
Fee config: ForecastEx `fee_per_side=0.01` (filing rules03242641956; check for an IBKR commission on top — if unknown, run with 0.01 and 0.02). CME event contract fees: look up the CME fee schedule; if not found, report break-evens instead of edges.

Per product:
1. Ladder quality: width, depth, strikes quoted, volume/trades seen.
2. **Mid gap (venue mid − fair)** by moneyness bucket (|ln K/F|/σ√T: <0.5, 0.5–1, 1–2, >2) and time-to-fix. Median, IQR, % of snapshots with |gap| > 2¢, > 3¢.
3. **Taker edge after fees**: % of snapshots with any strike > 1¢, > 2¢; `capturable_dollars` per snapshot at thresholds 0.01 / 0.02.
4. **Model-free arb edges** (bounds-based): count and $; any non-zero result gets its own table, with an explicit check for stale or bad option quotes.
5. Persistence: for strikes with gap > 3¢, how many consecutive snapshots it lasts.
6. Post-settlement window: is the book open after the fixing? Did anything trade at stale prices?
7. Expiry-mismatch share and its likely bias direction.
8. **Verdict block** using the decision rule below. Show the numbers that drove it.

### Decision rule (owner may edit; agents may not)
- **GO (selective quoting):** some moneyness bucket shows median |gap| ≥ 3¢ with ≥ 30% of snapshots > 3¢, persistence ≥ 3 snapshots, and median displayed size at those strikes ≥ 20 contracts.
- **INVESTIGATE:** gaps exist but sizes are tiny, or edge is concentrated in expiry-mismatched snapshots.
- **KILL (venue):** median |gap| < 2¢ in all buckets, or taker edge after fees ≤ 0 in > 90% of snapshots.

## Safety
- Must not import `ib_async` order classes or call any order method. Add a test that greps `spotcheck/` for `placeOrder|Order(`, and fail if found.
- Port 4002 only; refuse 4001.

## /goal (paste after approving the plan)
```
/goal `bmm spotcheck collect` and `bmm spotcheck report` exist; `make check` exits 0 with output shown, including a test proving spotcheck/ contains no order calls and refuses port 4001; a collect dry-run against a recorded fixture writes ladder/futures/options parquet (show `ls -R data/spotcheck`); `bmm spotcheck report` on that fixture produces reports/spotcheck_fixture.md containing all 8 report sections and a verdict block (print its headings). Do not modify src/bmm/spotcheck/analyze.py except to fix a failing test, and if you do, show the diff. quant-reviewer returns PASS on collect.py's normalisation and expiry-matching logic. Stop after 40 turns.
```
Live collection (2–3 sessions) is then run by the owner on the VPS: `bmm spotcheck collect --product forecastex-spx,forecastex-ndx,cme-gold`.
