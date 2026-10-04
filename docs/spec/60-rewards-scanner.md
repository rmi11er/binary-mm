# Spec 60: Rewards scanner (`bmm scan rewards`)

> Part of the binary-mm spec. Index: [`SPEC.md`](../../SPEC.md). Tier B (read-only tool). Safety: [`00-safety.md`](00-safety.md).

**Purpose.** Every 5 min, snapshot all markets with an active liquidity-reward program on public venues. Estimate the reward a quoting sleeve would earn per dollar of collateral, and allocate a notional $8k. Read-only: no order, auth or account code anywhere in `src/bmm/scan/`.

## Venues (one `VenueAdapter` protocol)
| Venue | Data used | If not public |
|---|---|---|
| Polymarket US | `GET /v1/incentives`, book/BBO, trade tape | skip venue, log reason |
| Kalshi | liquidity-incentive terms + books for eligible markets | terms webpage-only → `program_source="manual"`, skip venue |
| Gemini Predictions | liquidity-rewards config endpoints + books | skip venue, log reason |
| ForecastEx | no data pull; research note only: has a renewed program been announced? (result + source) | — |

Endpoints, auth, scoring formulas, maker fees/rebates and collateral rules come from official docs. They are recorded in `research/venue_facts.md` and `config/scan_venues.toml`, with unverified facts marked `[?]`. If an endpoint needs credentials, the venue is treated as not public. The scanner never loads `.env`.

## Unit of analysis: the half-market
Each `(market, side ∈ {bid, ask})` is modelled separately. Each has its own `R_side`, `D_side` (competing scored depth on that side), `depth_raw_side`, `coll_side` ($/contract to rest there; source URL; `coll_verified`), `fills_side` and markout.

The adapter owns the venue's scoring:
- `expected_reward(prog, s, side, book) → $/hr` for our `s` contracts at the best-scoring price on that side, given the competing book.
- `qualifying_min(prog, side, book) → contracts` = `max(venue_min, target_size − existing_side_depth)`, floored at `venue_min`.
- `s_star_closed_form(...)` is provided only where the Polymarket US pro-rata formula applies (`R_side·w·s/(w·s+D_side)`, with w our score weight): `s* = (sqrt(R_side·w·D_side/c) − D_side)/w`. Every other adapter returns None.

## Venue scoring as implemented (all assumptions are `*_verified = false` in `config/scan_venues.toml`)
- **pm_us:** `Score = discount^(ticks from best) × size`, pro-rata per side, walked out until `targetSize`. A tick is assumed to be $0.01. `rewardPool` is spread over 24h when `end` is null. There is **no public trade tape**, so `fills_lower_bound = true` and its halves stay in "pending". The rebate is 0.0125·p(1−p).
- **kalshi:** the same geometric form measured from a Reference Price (the first level where cumulative size ≥ target/5). Asks are derived from NO bids. Needs both sides, so it is allocated as a block. Trade direction comes from `taker_outcome_side`.
- **gemini:** the spread-weight curve is unpublished, so `(1 − d/max_spread)²` from the mid is an approximation. The pool is treated as shared across `pool_event_count` events, and the rebate is ignored. Both are conservative.
- Each adapter reads one page of programs and is capped by `max_markets`. The HTTP client paces requests per host and retries 429s with backoff.

## Snapshot (parquet, `data/scan/<table>/<YYYY-MM-DD>/<HHMMSS>.parquet`)
`snapshots` (one row per half-market): `ts_ns, venue, market_id, side, title, category, expiry_ns, R_side, target_size, max_spread, venue_min, discount, D_side, depth_raw_side, bid, ask, mid, spread, top_holder_share` (null if not exposed), `fills_side_hr` (**contracts**/hr that hit this side), `markout_30m_side` (size-weighted mean signed maker markout vs mid 30 min later, $/contract; negative = adverse), `n_trades_side, n_markout_side, fills_lower_bound, coll_side, coll_verified, maker_fee, maker_rebate, settle_source, settle_ns, reference_quality ∈ {venue_ref, model_needed, none}`. Separate tables: `programs` (raw terms + hash) and `trades`.

## Cost and allocation (all in $/hr per posted contract)
- `f = fills_side_hr / max(depth_raw_side, 1)`: contracts filled per hr per posted contract, a queue-share approximation and slightly conservative.
- Markout shrinkage: `m = w·m_mkt + (1−w)·prior`, with `w = min(n_markout_side, 20)/20`, where `n_markout_side` is the number of fills that have a +30m mid. The prior is the category mean (weighted by `n_markout_side`), falling back to the all-category mean, then to 0. The result is flagged `c_lower_bound` when there is no category prior and w < 1. Flagged markets stay out of the allocation and the top 25 and appear under "pending markouts".
- `cost_per_fill = max(0, max(0, −m) − maker_rebate + maker_fee)`
- `c = f·cost_per_fill + coll_side·0.05/8760`
- `EV(s) = expected_reward(s) − c·s`. The optimum is `s*` from the closed form if available, else a numeric integer argmax over `[q, s_cap]` (q = qualifying min, `s_cap = floor(0.15·8000/coll_side)`), assuming EV is unimodal above q.
- Qualifying rule: if `s* < q`, use `s = q` when `EV(q) > 0`, else 0.
- Two-sided programs: the first allocation step buys both sides' q together as one block. Each side then grows independently.
- Greedy allocation of $8,000 collateral (then one improvement pass: drop each funded market, refill greedily, keep the better total): start each market with its qualifying block, ranked by `EV(q)/(q·coll)`, then add 1 contract at a time at `(EV(s+1)−EV(s))/coll`. Stop a market at s\*, at 15%/market ($1,200) or when its venue hits 40% ($3,200). Two-sided programs are skipped unless both halves are present. Because of the blocks this is a heuristic. It is exact only when q = 1 and sides are unpaired.
- Per half-market output: `s, reward_day = 24·expected_reward(s), cost_day = 24·c·s, collateral = s·coll, roc_day`.

## Exclusions (listed with the reason, never silently dropped)
Live in-game sports; crypto with < 24h to expiry; `top_holder_share > 0.5` at the touch. Null `top_holder_share` is recorded as "concentration unknown" and **not** excluded.

## Report `reports/rewards_scan_<YYYY-MM-DD>.md` (`bmm scan report`)
1. Top 25 markets: bid+ask combined, with s per side, reward/day, collateral, RoC, `n_trades` per side and flags.
2. Breakdown by venue and category.
3. Domain sleeve candidates: `model_needed` markets in metals, energy, rates, FX and equity indices, ranked separately and never excluded for lacking a reference.
4. New, changed or ended programs since the previous scan (`programs` hash diff).
5. Exclusions with reasons.
6. Venue rules table (collateral, maker fee/rebate, scoring formula; source; verified/[?]) plus the ForecastEx program status and source.

## CLI
`bmm scan rewards [--once] [--interval 300] [--venues pm_us,kalshi,gemini]`, `bmm scan report [--date]`. Refuses to run if `BMM_MODE=live`.

## Safety acceptance
- One parametrized no-order-surface test, reusing `tests/spotcheck/test_safety.py::scan`, over `spotcheck` and `scan`. For `scan` it adds a rule forbidding HTTP POST/PUT/PATCH/DELETE, `/order(s)` paths (`orderbook` allowed) and auth headers.
- One port/env test: refuses `BMM_MODE=live`, `scan/` never imports `ib_async`, and the HTTP client rejects hosts outside the allowlist.
