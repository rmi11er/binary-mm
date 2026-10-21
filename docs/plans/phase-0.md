# Phase 0 — Venue Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Research tasks (2–9) are dispatched to the `venue-researcher` subagent; tasks 0, 1, 10, 11 run in the main session.

**Goal:** Resolve every `[?]` in the ForecastEx table of `research/venue_facts.md` (or mark it `needs owner` with a reason) and produce `research/product_shortlist.md` with ≥ 2 sourced candidates. This is the input that fixes F, T and the hedge for Phases 1–3.

**Architecture:** This is research only and adds no product code. Each research task starts by writing down its acceptance check, the "test", then fills the facts from primary sources, then runs the check. One shell checker (`research/check_phase0.sh`) encodes the SPEC Phase 0 acceptance criteria and is the final gate.

**Tech Stack:** Markdown, bash/awk/grep (macOS BSD-compatible), WebFetch/WebSearch via `venue-researcher`, git.

**Spec:** `SPEC.md` §7 Phase 0 (acceptance), §3 (safety), §4/§6.4/§9 (what the facts feed). Working rules: `CLAUDE.md`. Agent rules: `.claude/agents/venue-researcher.md`.

## Design decisions resolved during brainstorming (from the spec; no owner input needed)

1. **Phase-1-tagged rows.** "Market data" is researched from IBKR API docs. If it's not resolvable from docs, the row becomes `needs owner — confirm in Phase 1`. "Order-path latency" can only be measured, so it becomes `needs owner — measured in Phase 1 from the VPS`. SPEC acceptance permits this.
2. **Paper trading support.** Research the docs only. No connection to IB Gateway of any kind in Phase 0. If the docs don't settle it, mark it `needs owner` and add it to `research/owner_checklist.md`.
3. **Shortlist scope.** Equity-index (S&P 500, Nasdaq-100) and gold digitals on ForecastEx, plus CME Event Contracts on gold and NQ. ForecastEx crypto contracts are listed as **NO-GO (v1)** because §4 feeds and hedger cover only ES/MES/NQ/MNQ/GC/MGC. Econ-indicator and temperature products are **NO-GO** because they can't be priced off an options surface (§1 thesis). The final product list stays with the owner (§10.1).
4. **Retainer program.** Record the terms only. Whether to apply is the owner's call (§10.3).
5. **Incentives.** Record the latest effective terms. The shortlist notes the CFTC advisory 26-23 guidance to underwrite at ~half the current terms. Phase 0 makes no EV estimates (those are Phase 3).
6. **"Tests first" for a no-code phase.** Each task writes its acceptance check command before doing research. `research/check_phase0.sh` is written in Task 1 and must fail on today's files.
7. **No git repo exists yet.** Task 0 creates one so the per-branch workflow works.

## Global Constraints

- Modify only files under `research/` (plus the git bootstrap in Task 0). Do not touch `src/`, `tests/`, `config/`, `SPEC.md`, `CLAUDE.md`.
- Never run anything with `--live`, never set `BMM_MODE=live`, never connect to IB Gateway (ports 4001 **or** 4002) in Phase 0.
- Never read, print or commit `.env` or credentials.
- Source preference, in order: exchange rulebook/contract specs → CFTC filings (cftc.gov) → official IBKR API docs → official exchange blogs. Secondary press is `[S]` only.
- Every fact added: status `[V]`/`[S]`, one-line detail, source URL (`https://…`), `accessed 2026-MM-DD`.
- **Do not guess.** Never infer a reference price, settlement time, fee or rule by analogy with another venue (Kalshi, CME EC, Gemini). If it isn't found, mark the row `needs owner`, give the reason, and add `Searched:` listing the sources tried.
- Table cells must not contain a literal `|` character (it breaks the checker).
- Times always carry a timezone (ET/CT) **and** a note on DST handling if the source states one.
- `needs owner` row format: Status cell = `needs owner`; Detail cell = `<reason>. Searched: <sources tried>`.

## Review Focus

1. **Futures- vs cash-index reference (SPEC §6.4).** A row that says "S&P 500" without naming the exact instrument (ES front month? which roll rule? SPX cash?) silently breaks F and the hedge. Tasks 3–4 require the instrument, contract month/roll rule and settlement source as separate fields.
2. **Timezone/DST ambiguity in reference time.** "4:00 pm" without ET/CT, or with no DST statement, breaks T near resolution, where gamma explodes. Tasks 3–4 and 8 require a timezone on every time.
3. **Superseded filings.** Incentive and retainer terms were amended repeatedly (Aug and Sept 2026). Task 5 requires the effective date of the latest filing and a search for anything newer than rules09042624145.
4. **Marketing pages tagged `[V]`.** ForecastTrader marketing or Traders' Insight commentary must stay `[S]`. Each task's review step checks that every `[V]` cites a rulebook, CFTC filing or API doc URL.
5. **Facts by analogy.** For example, assuming ForecastEx nets YES+NO the way Kalshi does, or that CME EC incentives match ForecastEx's. Task 6 and Task 9 review steps explicitly reject any claim whose source is about a different venue.

---

### Task 0: Git bootstrap

**Files:**
- Create: `.git/` (via `git init`)

**Interfaces:**
- Produces: repo with `main` and a `phase-0` branch, which later tasks commit to.

- [ ] **Step 1: Write the check first**

```bash
git rev-parse --abbrev-ref HEAD   # expect: phase-0
git check-ignore -q .env && echo ".env ignored"                        # expect: .env ignored
git ls-files | grep -cE '(^|/)\.env$'                                   # expect: 0
```
Run it now. Expected: fails with "not a git repository".

- [ ] **Step 2: Init and commit**

```bash
git init -b main
git check-ignore -q .env || echo "STOP: .env not ignored — report to owner, do not commit"
git add -A && git status --short | grep -E '(^|/)\.env$' && echo "STOP" || true
git commit -m "chore: initial import of binary-mm scaffold"
git switch -c phase-0
```
Do not `cat` `.gitignore` entries that reference secrets beyond confirming `.env` is ignored. Never open `.env`.

- [ ] **Step 3: Re-run Step 1 checks.** Expected: `phase-0`, `.env ignored`, `0`.

---

### Task 1: Acceptance checker (the "test")

**Files:**
- Create: `research/check_phase0.sh`

**Interfaces:**
- Produces: `bash research/check_phase0.sh [venue_facts.md] [product_shortlist.md]`. Exits 0 iff SPEC Phase 0 acceptance holds, and prints each violation on its own line. Later tasks run this to see progress.

- [ ] **Step 1: Write the checker**

```bash
#!/usr/bin/env bash
# Phase 0 acceptance check (SPEC.md §7 Phase 0). Exit 0 only if all criteria hold.
set -u
VF=${1:-research/venue_facts.md}
SL=${2:-research/product_shortlist.md}
fail=0

# --- ForecastEx table in venue_facts.md ---
fx=$(awk '/^## ForecastEx/{on=1;next} /^## /{on=0} on' "$VF")
open=$(printf '%s\n' "$fx" | grep -c '\[?\]')
echo "ForecastEx [?] remaining: $open"
if [ "$open" -ne 0 ]; then
  printf '%s\n' "$fx" | grep '\[?\]' | cut -d'|' -f2 | sed 's/^/  OPEN:/'
  fail=1
fi

while IFS= read -r row; do
  item=$(printf '%s' "$row" | cut -d'|' -f2)
  status=$(printf '%s' "$row" | cut -d'|' -f3)
  detail=$(printf '%s' "$row" | cut -d'|' -f4)
  src=$(printf '%s' "$row" | cut -d'|' -f5)
  case "$status" in
    *'[V]'*|*'[S]'*)
      printf '%s' "$src" | grep -q 'https://' || { echo "  NO URL:$item"; fail=1; }
      printf '%s' "$src" | grep -q 'accessed 2026-' || { echo "  NO ACCESS DATE:$item"; fail=1; } ;;
    *'needs owner'*)
      printf '%s' "$detail" | grep -q 'Searched:' || { echo "  NEEDS-OWNER WITHOUT 'Searched:':$item"; fail=1; } ;;
    *'[?]'*) : ;;  # already reported above
    *) echo "  BAD STATUS:$item"; fail=1 ;;
  esac
done < <(printf '%s\n' "$fx" | grep '^| ' | grep -v '^| Item ')

# --- product_shortlist.md ---
if [ ! -f "$SL" ]; then
  echo "MISSING: $SL"; fail=1
else
  n=$(grep -c '^### ' "$SL")
  echo "Shortlist candidates: $n"
  [ "$n" -ge 2 ] || { echo "  FEWER THAN 2 CANDIDATES"; fail=1; }
  for f in 'Venue:' 'Reference price:' 'Reference time:' 'Strike grid:' \
           'Incentive eligibility:' 'Hedge:' 'Market data:' 'Decision:' 'Sources:'; do
    c=$(grep -c "^- $f" "$SL")
    [ "$c" -eq "$n" ] || { echo "  FIELD '$f' present $c/$n"; fail=1; }
  done
  d=$(grep -cE '^- Decision: (GO|NO-GO|NO-GO \(v1\)) — ' "$SL")
  [ "$d" -eq "$n" ] || { echo "  DECISION malformed in $((n-d)) candidate(s)"; fail=1; }
  awk '/^### /{if(name!="" && !url) print "  NO URL in candidate: " name; name=$0; url=0}
       /https:\/\//{url=1}
       END{if(name!="" && !url) print "  NO URL in candidate: " name}' "$SL" | grep . && fail=1
fi

[ "$fail" -eq 0 ] && echo "PHASE 0 ACCEPTANCE: PASS" || echo "PHASE 0 ACCEPTANCE: FAIL"
exit "$fail"
```

- [ ] **Step 2: Run against today's files and verify it fails for the right reasons**

Run: `bash research/check_phase0.sh; echo "exit=$?"`
Expected: `ForecastEx [?] remaining: 6`; six `OPEN:` lines (reference price/time, incentive schedule, paper trading, netting/collateral, order-path latency, market data); `NO URL:` and `NO ACCESS DATE:` lines for each of the 9 existing `[V]`/`[S]` rows; `MISSING: research/product_shortlist.md`; `FAIL`; `exit=1`.

- [ ] **Step 3: Verify it can pass, using a fixture in the scratchpad**

```bash
T=$(mktemp -d)
cat > "$T/vf.md" <<'EOF'
## ForecastEx (primary target)
| Item | Status | Detail | Source |
|---|---|---|---|
| A | [V] | fact | https://example.org accessed 2026-10-03 |
| B | needs owner | Requires account. Searched: IBKR docs | — |
## Other
| X | [?] | outside section | — |
EOF
cat > "$T/sl.md" <<'EOF'
### One
- Venue: v
- Reference price: p
- Reference time: t
- Strike grid: g
- Incentive eligibility: i
- Hedge: h
- Market data: m
- Decision: GO — reason
- Sources: https://example.org
### Two
- Venue: v
- Reference price: p
- Reference time: t
- Strike grid: g
- Incentive eligibility: i
- Hedge: h
- Market data: m
- Decision: NO-GO — reason
- Sources: https://example.org
EOF
bash research/check_phase0.sh "$T/vf.md" "$T/sl.md"; echo "exit=$?"
```
Expected: `PHASE 0 ACCEPTANCE: PASS`, `exit=0`. The `[?]` outside the ForecastEx section is ignored. Then edit the fixture so row B has no `Searched:` and re-run. Expected: `FAIL`.

- [ ] **Step 4: Commit**

```bash
chmod +x research/check_phase0.sh
git add research/check_phase0.sh
git commit -m "research: add Phase 0 acceptance checker"
```

---

### Task 2: ForecastEx financial product inventory

**Files:**
- Create: `research/forecastex_inventory.md` (working notes feeding the shortlist)

**Interfaces:**
- Produces: a table `| Product | Category | Reference (as stated) | Listing cadence | Contract-spec URL |` covering every **currently listed** ForecastEx financial product (index, metals, rates, FX, crypto). Tasks 3, 4 and 10 read it.

- [ ] **Step 1: Write the check first**

```bash
grep -c '^| ' research/forecastex_inventory.md     # expect >= 3 (header + >= 2 products)
grep -c 'https://' research/forecastex_inventory.md # expect >= number of product rows
```
Run now. Expected: file missing.

- [ ] **Step 2: Research.** Sources to try in order: forecastex.com contract specifications and rulebook; the cftc.gov DCM filings list for ForecastEx (self-certified contract terms); IBKR ForecastTrader product listing; IBKR TWS API "Event Trading" pages. Record each product's own wording for its reference ("S&P 500 futures", "S&P 500 index", etc.) verbatim-paraphrased. Do not normalize it.

- [ ] **Step 3: Run the check.** Expected: both counts satisfied.

- [ ] **Step 4: Review.** Every row cites a ForecastEx or CFTC URL, not only IBKR marketing. Crypto rows are present but flagged `v1 out of scope (hedger)`.

- [ ] **Step 5: Commit** `git add research/forecastex_inventory.md && git commit -m "research: ForecastEx financial product inventory"`

---

### Task 3: S&P 500 contract — reference price, time, settlement, strikes

**Files:**
- Modify: `research/venue_facts.md`, ForecastEx table row "Exact reference price and time of S&P / NDX contracts" (split into two rows, `S&P reference` and `NDX reference`; this task fills the S&P row)

**Interfaces:**
- Consumes: S&P product rows and spec URLs from `research/forecastex_inventory.md`.
- Produces: an S&P row whose Detail states all of the following, each sourced: **instrument** (e.g. specific ES contract or SPX cash), **contract month / roll rule**, **observation timestamp with timezone**, **settlement value definition** (last trade / VWAP over window / official settle / index print), **resolution and payout time**, **strike grid** (spacing, how many, listing time), **fallback if no reference print** (holiday/halt). Phases 1–2 consume this for F, T and the hedge (SPEC §6.3–6.4).

- [ ] **Step 1: Write the check first**

```bash
row=$(grep '^| S&P reference' research/venue_facts.md)
for k in 'Instrument:' 'Roll:' 'Observation:' 'Settlement value:' 'Resolution:' 'Strikes:' 'Fallback:'; do
  printf '%s' "$row" | grep -q "$k" || echo "MISSING $k"
done
printf '%s' "$row" | grep -qE '(ET|CT)' || echo "MISSING timezone"
```
Run now. Expected: row missing, so every key is reported.

- [ ] **Step 2: Research.** In order: the ForecastEx contract specification / rule appendix for the S&P product; the CFTC self-certification filing for that contract; IBKR TWS API ForecastEx contract docs (for `lastTradeDateOrContractMonth` and resolution timestamp semantics). If instrument or settlement is not stated in a primary source, mark the row `needs owner`, give the reason and add `Searched:`. Do **not** fill it from Kalshi/CME analogues.

- [ ] **Step 3: Run the check.** Expected: no output. (If the row is `needs owner`, the check is replaced by `bash research/check_phase0.sh`, which shows no `NEEDS-OWNER WITHOUT 'Searched:'` for it.)

- [ ] **Step 4: Review (Review Focus 1, 2, 4).** The instrument is a specific contract, not "the S&P"; every time has a timezone; `[V]` cites a rulebook or CFTC URL.

- [ ] **Step 5: Commit** `git commit -am "research: S&P ForecastEx reference/settlement spec"`

---

### Task 4: Nasdaq-100 (and any ForecastEx gold) contract — same fields

**Files:**
- Modify: `research/venue_facts.md`, ForecastEx row `NDX reference`. If Task 2 found a ForecastEx gold product, add a row `Gold reference` with the same fields.

**Interfaces:**
- Consumes: inventory from Task 2. Uses the same field keys as Task 3.
- Produces: rows with keys `Instrument: Roll: Observation: Settlement value: Resolution: Strikes: Fallback:`.

- [ ] **Step 1: Write the check first.** Same as Task 3 Step 1, with `'^| NDX reference'` (and `'^| Gold reference'` if added). Run it. Expected: missing keys.
- [ ] **Step 2: Research.** Same source order as Task 3. Treat NDX independently. Do not assume it mirrors the S&P product's rules.
- [ ] **Step 3: Run the check.** Expected: no output.
- [ ] **Step 4: Review (Review Focus 1, 2, 4).**
- [ ] **Step 5: Commit** `git commit -am "research: NDX (and gold) ForecastEx reference spec"`

---

### Task 5: Liquidity Incentive Program + retainer terms

**Files:**
- Modify: `research/venue_facts.md`, ForecastEx row "Incentive schedule"; update the "Liquidity Retainers" row only if a newer filing changes it.

**Interfaces:**
- Produces: an "Incentive schedule" Detail with keys `Eligible products:`, `Pool per period:`, `Period:`, `Scoring:` (distance-to-mid, size caps, two-sided requirement, uptime), `Report URL:`, `Effective:` (date of latest governing filing), `Latest filing checked:`. Task 10 reads it.

- [ ] **Step 1: Write the check first**

```bash
row=$(grep '^| Incentive schedule' research/venue_facts.md)
for k in 'Eligible products:' 'Pool per period:' 'Period:' 'Scoring:' 'Report URL:' 'Effective:' 'Latest filing checked:'; do
  printf '%s' "$row" | grep -q "$k" || echo "MISSING $k"
done
```
Run now. Expected: all missing.

- [ ] **Step 2: Research.** Start from CFTC filings rules08182618341/2, rules0707269158, rules08282622743 and rules09042624145. Then search cftc.gov ForecastEx filings dated **after 2026-09-04** for amendments (Review Focus 3). Get the daily incentive report URL from ForecastEx. If pool $ is not published per product, say so explicitly. "Not published" is a resolved fact if a primary source shows the report format.

- [ ] **Step 3: Run the check.** Expected: no output.

- [ ] **Step 4: Review.** `Effective:` is the latest filing date found. Retainer facts are recorded without any recommendation (§10.3).

- [ ] **Step 5: Commit** `git commit -am "research: ForecastEx incentive schedule and latest filings"`

---

### Task 6: Netting / collateral (+ interest on positions)

**Files:**
- Modify: `research/venue_facts.md`, ForecastEx row "Netting / collateral"; optionally upgrade "Interest on positions" from `[S]` to `[V]`.

**Interfaces:**
- Produces: Detail with keys `Auto-net YES+NO same strike:` (yes/no), `When:` (immediately / end of day / at settlement), `Collateral per contract:`, `Cash released on net:`. This feeds the §3.4 "gross collateral at risk" limit semantics and Phase 4 hedge-cost modelling.

- [ ] **Step 1: Write the check first** (same loop pattern as Task 5 with the four keys above). Run it. Expected: all missing.
- [ ] **Step 2: Research.** ForecastEx rulebook (clearing / settlement chapters), the ForecastEx DCO filings on cftc.gov, IBKR "Event Contracts" margin/collateral page. Same for interest on positions (rate basis, holding threshold).
- [ ] **Step 3: Run the check.** Expected: no output.
- [ ] **Step 4: Review (Review Focus 5).** Reject any claim sourced from Kalshi, Polymarket or CME EC documentation.
- [ ] **Step 5: Commit** `git commit -am "research: ForecastEx netting and collateral"`

---

### Task 7: Paper trading, market data, latency + owner checklist

**Files:**
- Modify: `research/venue_facts.md`, ForecastEx rows "Paper trading support", "Market data", "Order-path latency"
- Create: `research/owner_checklist.md`

**Interfaces:**
- Produces: `research/owner_checklist.md`, a numbered list of actions only the owner can take, each with *why it matters* and *which phase it blocks*. At minimum: confirm ForecastEx contracts resolve/trade in the IBKR paper account (if unresolved), confirm ForecastEx trading permission on the live account, confirm non-professional market-data status (SPEC §9), and price the CME data subscriptions needed by the shortlist. The Market data row's Detail uses keys `Top of book:`, `Depth (reqMktDepth):`, `Update cadence:`, `Data permission/cost:`.

- [ ] **Step 1: Write the check first**

```bash
for item in 'Paper trading support' 'Market data' 'Order-path latency'; do
  grep "^| $item" research/venue_facts.md | grep -qE '\[V\]|\[S\]|needs owner' || echo "OPEN $item"
done
test -f research/owner_checklist.md || echo "MISSING owner_checklist"
```
Run now. Expected: three `OPEN` lines plus `MISSING`.

- [ ] **Step 2: Research.** IBKR TWS API docs (ForecastEx pages, market data and market depth pages, paper trading account limitations page) and the IBKR Knowledge Base on paper-account product support. **Do not connect to any IB Gateway or TWS instance.** Set Latency to `needs owner`, with Detail `Requires measurement from VPS via paper account; scheduled Phase 1 latency probe. Searched: <any published IBKR latency figures>`.

- [ ] **Step 3: Run the check + `bash research/check_phase0.sh`.** Expected: no `OPEN` lines. The checker shows no `NEEDS-OWNER WITHOUT 'Searched:'` for these rows.

- [ ] **Step 4: Commit** `git add research/owner_checklist.md && git commit -am "research: paper/market data/latency status + owner checklist"`

---

### Task 8: Re-verify and source-URL the existing ForecastEx rows

**Files:**
- Modify: `research/venue_facts.md`, the 9 pre-existing ForecastEx rows (API access, YES/NO mapping, Order side, Tick/payout, Commission, Financial products, Interest on positions, Liquidity Incentive Program, Liquidity Retainers; skip any already URL'd in Tasks 5–6).

**Interfaces:**
- Produces: every `[V]`/`[S]` row has an `https://` URL and `accessed 2026-MM-DD`. Status is downgraded to `[S]` if only secondary sources confirm it. If a primary source **contradicts** a row, edit it and note `(corrected 2026-MM-DD; was: …)`.

- [ ] **Step 1: Write the check first.** Run `bash research/check_phase0.sh 2>&1 | grep -E 'NO URL|NO ACCESS DATE'`. Expected now: one line per un-URL'd row.
- [ ] **Step 2: Research.** Find the primary URL for each existing claim. Pay particular attention to "Commission: zero". Look for the ForecastEx/IBKR fee page, and if a non-zero exchange or clearing fee exists, record it. This flows into `src/bmm/pricing/fees.py` in a later phase via a PR, not here.
- [ ] **Step 3: Run the check.** Expected: no output.
- [ ] **Step 4: Commit** `git commit -am "research: source URLs for existing ForecastEx facts"`

---

### Task 9: CME Event Contracts (gold, NQ) specs + incentives (time-boxed)

**Files:**
- Modify: `research/venue_facts.md`, CME Event Contracts table (add rows `Gold EC reference`, `NQ EC reference` with the Task 3 keys; resolve or annotate "Incentives").

**Interfaces:**
- Produces: CME EC rows with keys `Instrument: Roll: Observation: Settlement value: Resolution: Strikes: Fallback:`, plus a fee row (`Exchange fee:`, `IBKR commission:`). Task 10 reads them.

- [ ] **Step 1: Write the check first.** Task 3's key loop, applied to `'^| Gold EC reference'` and `'^| NQ EC reference'`. Run it. Expected: missing.
- [ ] **Step 2: Research.** CME Rulebook chapters for Event Contracts, CME product pages, CME Special Executive Reports / MM program notices, IBKR commission page for CME EC. **Time-box incentives to 30 minutes.** If not found, leave `[?]` with `Searched:`. That's allowed, because this table is outside the acceptance criterion.
- [ ] **Step 3: Run the check.** Expected: no output.
- [ ] **Step 4: Review (Review Focus 5).** Nothing copied from ForecastEx rules.
- [ ] **Step 5: Commit** `git commit -am "research: CME event contract specs (gold, NQ)"`

---

### Task 10: `research/product_shortlist.md`

**Files:**
- Create: `research/product_shortlist.md`

**Interfaces:**
- Consumes: Tasks 2–9 outputs.
- Produces: one `### <Venue> — <Product>` block per candidate in exactly this field format (the checker parses it):

```markdown
### ForecastEx — S&P 500 daily close
- Venue: ForecastEx (via IBKR, secType OPT, exchange FORECASTX)
- Reference price: <instrument + settlement value definition, from venue_facts>
- Reference time: <timestamp with TZ; resolution/payout time>
- Strike grid: <spacing, count, listing time>
- Incentive eligibility: <yes/no; pool $ per period if published; underwrite at ~50% per CFTC advisory 26-23>
- Hedge: <MES/MNQ/MGC + basis note per SPEC §6.4: futures-referenced => no carry; cash-referenced => F = futures − fair basis>
- Market data: <IBKR subscriptions needed (futures + options surface) and $/month, or "owner to confirm" with checklist #>
- Decision: GO — <one-sentence reason>
- Sources: <https://… URLs>
```

Candidates must include: ForecastEx S&P, ForecastEx NDX, ForecastEx gold (if listed), CME EC gold, CME EC NQ, ForecastEx crypto (`NO-GO (v1) — hedger/feeds out of scope (SPEC §4)`), and one block for econ/temperature as a group (`NO-GO — no options surface to price from`). A `## Budget` section at the end sums the data subscriptions for the GO set against the $150/month cap (SPEC §1). Mark it `owner to confirm` where prices are account-dependent. No EV estimates.

- [ ] **Step 1: Write the check first.** Run `bash research/check_phase0.sh`. Expected: `MISSING: research/product_shortlist.md`.
- [ ] **Step 2: Write the shortlist** from venue_facts. Every claim must trace to a venue_facts row. No new facts get introduced here.
- [ ] **Step 3: Run the check.** Expected: no shortlist-related violations, and `Shortlist candidates: ≥ 2`.
- [ ] **Step 4: Review.** Any candidate whose reference is `needs owner` can't be `GO`. It becomes `NO-GO — pending owner item #N` instead. Decisions don't pre-empt §10.1. The shortlist is input to Phase 3, not a final list.
- [ ] **Step 5: Commit** `git add research/product_shortlist.md && git commit -m "research: product shortlist"`

---

### Task 11: Phase 0 gate

**Files:** none modified (verification only)

- [ ] **Step 1: Run the goal proofs and print the output into the transcript**

```bash
bash research/check_phase0.sh; echo "exit=$?"
awk '/^## ForecastEx/{on=1;next} /^## /{on=0} on' research/venue_facts.md | grep -c '\[?\]'   # expect 0
cat research/product_shortlist.md
git diff --stat main..phase-0 -- . ':!research'   # expect empty: nothing outside research/
```
Expected: `PHASE 0 ACCEPTANCE: PASS`, `exit=0`, `0`, the shortlist printed, empty diff stat.

- [ ] **Step 2: Summarize for the owner.** List what was resolved, every `needs owner` item with its checklist number, any correction to a pre-existing fact (especially fees, which would change `pricing/fees.py` later), and anything that changes SPEC §6.4 (futures vs cash basis). Stop, and don't merge without owner review.
