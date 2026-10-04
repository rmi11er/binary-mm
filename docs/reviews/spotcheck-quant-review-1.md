# Quant review (round 1): `spotcheck` branch (HEAD 55997fe vs `main`)

Reviewer: quant-reviewer subagent · 2026-10-04 · read-only

**Verdict: FAIL.** There is one Critical finding. The arithmetic is sound throughout, but with realistic live fetch timings the verdict pipeline almost never lets GO through. The fix is small and touches metrics and reporting only. No pinned constants change.

**Evidence**
- `uv run pytest -q`: 305 passed.
- The fixture report regenerates byte-identically (apart from PNG paths) from `uv run bmm spotcheck collect --fixture … && uv run bmm spotcheck report …` into the scratchpad.
- Three scratch scripts were used (not committed):
  - an analytic check of how often snapshots get flagged non-simultaneous;
  - a perturbation test of persistence on the fixture;
  - a hand-built multi-session frame for `post_fix_activity`.

---

## Critical

**C1. In live data the simultaneous bucket will be a minority, and non-simultaneous snapshots break persistence runs, so GO is practically unreachable.**
- **Where:**
  - `src/bmm/spotcheck/metrics.py:420-425`: `hit` requires `r["sim_bucket"] == sim`, so a non-sim row ends the run.
  - `src/bmm/spotcheck/collect.py:41,296-300`: flag when |dF| × digital delta at the ATM ladder strike > 1c.
  - `src/bmm/spotcheck/report.py:208-211`, which feeds `rule.verdict`.
- **How often snapshots get flagged:** the flag fires when 0.4·√(dt/T)·|Z| > 0.01. This depends only on the fetch interval dt and the time to fix T, not on the product. For ES (σ = 0.15):

  | T to fix | flag threshold | P(flag), dt = 5 s | dt = 15 s | dt = 30 s |
  |---|---|---|---|---|
  | 4h | 0.52 pt | 18% | 44% | 58% |
  | 30m | 0.185 pt | 63% | 78% | 85% |
  | 10m | 0.107 pt | 78% | 87% | 91% |

  Inside 10 minutes the threshold is below one ES mid increment (0.125 pt).
- **The fetch interval will be long.** The interval between the two future quotes covers the ladder, `reqSecDefOptParams`, one `reqContractDetails` per expiry and class in a 7-day horizon (`ibsource.py:249-285`, re-run every snapshot), then at least two qualify-plus-ticker rounds. That is realistically 10–40 s.
- **Effect on the fixture's designed GO:** I randomly re-tagged pre-fix snapshots as non-simultaneous.
  - Base: GO, median run 8.5.
  - 30% re-tagged: INVESTIGATE, median run 2.0 ("gaps without persistence").
  - 50% re-tagged: INVESTIGATE, median run 1.0.
  - 70% re-tagged: INVESTIGATE, median run 1.0.
- **The info line can't rescue it.** It reuses simultaneous-bucket persistence.
- **Failure scenario:** a venue that really is 4c rich in the 1–2σ bucket, with size and persistence, is reported INVESTIGATE every session. The one-off goal of a decisive GO or KILL is not met.
- **Fix:** metrics and reporting only, no constant changes.
  1. Persistence for the verdict: a non-simultaneous snapshot should count as a hit when |gap_outside| > thr, and break the run only when gap_outside is null or ≤ thr. That is the "adjusted" series `gap_stats(sim="adjusted")` already uses, and it is conservative because |gap_outside| ≤ |gap|. Keep mismatched and missing snapshots breaking runs.
  2. Recompute `sim_bucket` in metrics from the stored `und_px_before`/`und_px_after`, sigma, `T_fix` and the ATM ladder strike, with the threshold as a report parameter. The owner can then re-classify without recollecting. Show the simultaneous share by ttf bucket in §2.
  3. Cache `option_expiries` and `listed_strikes` per (product, session). They don't change intraday, and caching cuts dt and the flag rate.
  4. Add a fixture or test where the virtual clock advances per source call. Today every fetch interval is exactly 0: I checked `fut_ts_ns − fut_before_ts_ns` = 0 and `opt_ts − ts` = 0. Add a scenario with about 60% non-simultaneous snapshots and assert that spx stays GO.
  5. Flag for the owner: whether the label itself should use the adjusted series (the ledger ruling already lists this as "cost if wrong").

## Important

**I1. Arb `suspect` will be True on essentially every live row, which makes §4 uninformative.**
- **Where:** `metrics.py:42,477,491`.
- **Problem:** `lag = |ladder_ts − opt_ts|`, where `opt_ts` is when the option-chain fetch completed. In the live sequence (ladder → expiries → strikes → two or more call batches) that is routinely more than 5 s. The fixture never exercises it, because its lag is 0.
- **Why it's redundant:** non-simultaneity is now handled by w.
- **Fix:** mark a row suspect on lag only when w is null, or when the lag exceeds a much larger bound (for example 60 s). Keep `opt_ts_lag_ms` as an information column.

**I2. §6 post-settlement numbers are wrong in multi-session reports and miss trades.** Each of the following was confirmed with a scratch frame.
- (a) `metrics.py:549-552`: `pre_fair` is grouped by `(product, strike)` only. Day-1 post-fix trades get compared with the last pre-fix fair of the last day. In my test the day-1 trade at 0.99 should read +0.39 against its own pre-fix fair of 0.60; it was reported +0.89 against day 2's 0.10. Fix: group and join on `fix_ts_ns` too.
- (b) `normalise.py:35-38,63`: `clean(volume)` maps 0 to None. `_volume_steps` (`metrics.py:504-508`) diffs non-null volumes only, so the first trade at a strike that had no prior volume is never counted (in my test 2 trades / 9 contracts became 1 / 4). That first trade is exactly the "anything traded after the fix?" case. Fix: treat volume as a size (0 is valid, only nan or negative becomes None), or count the first non-null volume as a step.
- (c) `metrics.py:507`: `.over("product","strike")` diffs across sessions. Use `("product","fix_ts_ns","strike")`.
- (d) `collect.py:109-110`: `last` and `volume` come from the YES/Call ticker only. On cme-gold, trades on the NO (Put) book are invisible. Log the put's last and volume too, and sum them. For ForecastEx, whether a matched YES/NO trade shows on both tickers is unknown; tag it `[?]` in venue_facts.
- (e) Suggestion: alongside `px_vs_fair`, report the price against the settled outcome 1{F_fix > K}, using the future at the first post-fix snapshot. That answers "traded at stale prices" directly.

## Minor

**M1. Collect and metrics choose the delta sigma differently** (the implementer flagged this).
- `collect.py:285-290` uses match, else `sigmas[0]` (the "below" chain when bracketing), else the memo. `metrics.py:112-135,178-180` uses match, else the chain nearest the fix.
- In a bracketing snapshot, `df_flag` (the bucket) and w (the widening) can use different sigmas.
- Fix: write the sigma used for the flag into the futures row (`df_sigma`) and use it in metrics. Alternatively, recompute the flag in metrics (C1 fix 2).

**M2. The informational verdict is asymmetric.**
- `metrics.py:261-266` and `rule.py:110-122`: gap_outside shrinks |gap|. That is conservative for GO but anti-conservative for KILL's "median < 2c in every bucket".
- Fix: state this in the info reason, or evaluate KILL with the unadjusted mid_gap.

**M3. w is a heuristic, not replication-grade.**
- `metrics.py:191-195`: w = |Δ(F̄, K, σ, T_fix)|·|F_after − F_before|.
  - It uses the endpoint move, not the excursion within the interval.
  - It has no gamma or skew term.
  - On the first snapshot the ladder sits before `f_before` (up to 5 s) and is not bracketed.
- The arb section text says "replication-grade" and "model-free". Add "approximately, after a delta widening for the futures move".

**M4. First-snapshot re-quote on a different con_id is used as `f_after`.**
- `collect.py:250`: with `cached is None` and a roll mid-snapshot, dF becomes a calendar spread.
- Fix: set `f_after = None` (null means flagged) whenever `fut2.con_id != fut.con_id`.

**M5. Tick floors drive the ">2" bucket.**
- Fixture: spx 1.46c, ndx 1.48c, from venue floors (bid ≥ 0.01, ask ≥ bid + 0.01, so mid ≥ 1.5c against fair ≈ 0). In live data, wings quoted 0.01/0.03 would block KILL's gap clause with a gap nobody can trade.
- Fix: report the ">2" bucket with and without rows where fair < 0.01 or > 0.99. Owner to decide whether the rule should see it.

**M6. §7 doesn't state the expected tilt sign.**
- `report.py:361-363`: add "a later-expiry (`above`) chain should show tilt > 0, an earlier (`below`) one < 0, if the mismatch drives the gap". I verified that sign from N(d2) with larger total variance.

**M7. Stale or inaccurate wording.**
- `metrics.py:401-402`: the persistence docstring still says "df_flag row breaks a run".
- `report.py:231-233`: "Post-fix rows carry no option rows". Mismatched "above" chains do have post-fix rows; they're deliberately ignored.
- The ledger's deferred note ("no futures row ⇒ simultaneous" stated in §2) is in the metrics docstring only.

**M8. A stray file is committed:** `tests/spotcheck/test_rule.py.bak`. It is the pre-55997fe version of `test_rule.py` and differs only in the "no data" strings and the two new tests. Delete it.

## Checked and correct
- **analyze.py (frozen; sha OK):**
  - The slope is the average of D over the interval.
  - The upper bound uses an interval ending ≤ K, (C_ask(a) − C_bid(b))/(b − a). The long call spread super-replicates 1{F > K}.
  - The lower bound mirrors it.
  - The point fair interpolates between the two midpoints with weight in [0, 1], so it lies inside [lower, upper].
  - The skew is captured model-free.
  - `capturable_dollars` agrees in meaning with `metrics.taker_stats` (strict threshold, taker-side size).
- **Normalisation:**
  - ForecastEx: yes_bid = YES bid, yes_ask = 1 − NO bid, ask_size = NO bid size.
  - Size-0 sides and nan / −1 become None.
  - Crossed and locked rows use an ε tolerance.
  - CME combines the call and put books with the correct max/min, and the scale guards work in both directions.
- **Expiry selection:**
  - Only chains on the underConId of the reference future are used.
  - A match is within ±60 s; otherwise the bracketing expiries within 2 days.
  - EC* and the product's own class are always excluded (the decoy ECGC is never chosen).
  - Already-expired chains are skipped.
- **Reference future:** lead_quarterly and the final-settlement roll are correct, with a month cross-check; named_gc parsing is correct; under_con_id is correct.
- **Sigma and T:**
  - Calendar-year T everywhere.
  - IV is inverted with T to the chain's own expiry from the slot time; moneyness and delta use T to the fix from the same slot time, so total variance is consistent.
  - `digital_delta` = n(d2)/(Fσ√T) matches finite differences to 1e-9.
  - It is finite as T→0 (12.9 per pt at T = 1e-9 ATM, 0 off-strike); no NaN.
  - Post-fix rows get w = null and `df_flag` = False.
- **Future-before-ladder:**
  - The call order is future → ladder → options → future from snapshot 2 on.
  - Roll invalidation recurses at most once.
  - A failed or priceless re-quote gives `und_px_after` = None and `df_flag` = null, which counts as non-simultaneous, with w = null, no gap_outside, and arb rows suspect.
- **Widening:**
  - w is computed at each row's own strike.
  - `lower_w`/`upper_w` are clipped to [0, 1].
  - The arb edges and arb $ use the widened band (× payout_scale).
  - `gap_outside` = sign · max(|gap| − w, 0).
- **Buckets:**
  - Moneyness is |ln K/F|/(σ√T_fix) with bins <0.5 / 0.5–1 / 1–2 (≤2 inclusive) / >2.
  - "n/a" is excluded by the rule.
  - The ttf buckets are correct.
- **Taker stats:**
  - The best edge is the per-snapshot max over strikes.
  - The fee shift is linear and correct (0.01 is derived from the 0.02 frame).
  - Capturable $ is reported at thresholds 0 / 0.01 / 0.02, × payout_scale.
- **Persistence (apart from C1):** the grid ordinals stop runs crossing sessions, mismatched or missing snapshots break runs, and medians are computed correctly.
- **Post-fix fair:** always taken from the last pre-fix chain of the same (product, fix). The mismatch share and tilt are correct apart from M6.
- **Rule against the owner's reading O2:**
  - Statistics are per observation within a bucket.
  - Persistence is the median run.
  - Taker edge is the best strike's.
  - Order is KILL → GO → INVESTIGATE.
  - GO uses matched-only gaps, persistence and sizes.
  - The verdict fee is 0.02.
  - Strict and inclusive comparisons match the spec.
  - An empty simultaneous bucket gives INVESTIGATE.
  - The constants are pinned verbatim and the test is green.
- **Fixture verdicts are not artefacts of metric choices:**
  - spx GO comes from the designed ±4c in the 1–2 bucket at size 40.
  - ndx KILL holds on both clauses independently (gap 1.48c; taker ≤ 0 in 100% of snapshots).
  - gold INVESTIGATE is driven by size 3.
  - cme-gold INVESTIGATE comes from every snapshot being on the fix + 24h chain.
  - Caveat: the fixture has zero fetch latency, so it cannot reveal C1 or I1.
- **Report coverage:** all 8 spec sections are present per product, plus a Summary. Both fee runs are shown; gaps are given per observation and per snapshot; the arb table has count, $ and suspect split.
- **Other files:** `clock.py` is DST-correct and the fix times are not ambiguous. The `cli.py` dispatcher is fine. The venue_facts addition is correctly tagged `[?]`. `ib_async` is capped `<3`.

## pricing/ cosmetic edits (e78c8d4)
They are behaviour-neutral, which I confirmed:
- `bmm.pricing.__all__` has the same 16 names as on `main` (set equality checked); only the order changed.
- `digital.py` swaps `typing.Callable` for `collections.abc.Callable`, used only in an annotation under `from __future__ import annotations`.
- `tests/test_pricing.py` only re-wraps imports and drops an unused `# noqa: E731`.
- Ruff is clean and the pricing tests pass.
