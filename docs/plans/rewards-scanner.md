# Rewards Scanner Implementation Plan

> **For agentic workers:** Execution is **inline** (superpowers:executing-plans) on the sonnet session model. **Only Task 1** goes to a subagent (venue-researcher). There is one quant-reviewer pass, on `allocator.py` (Task 5). Steps use checkbox (`- [ ]`) syntax.

**Goal:** `bmm scan rewards` snapshots every rewarded market on public venues every 5 min. `bmm scan report` writes `reports/rewards_scan_<date>.md`, which includes a $8k collateral allocation over half-markets.

**Architecture:** `src/bmm/scan/` contains one `VenueAdapter` protocol (data + scoring: `expected_reward`, `qualifying_min`, optional closed-form s\*) and one module per venue. All venues use a GET-only, host-allowlisted HTTP client. Snapshots, books and trades go to parquet under `data/scan/`. Cost, shrinkage and the allocator are pure functions in `allocator.py`. The report rebuilds the per-side reward functions from the stored books and does no network I/O.

**Tech Stack:** Python 3.12, asyncio, httpx (new dep, GET only), polars/pyarrow, pytest.

**Spec:** `docs/spec/60-rewards-scanner.md` (+ `docs/spec/00-safety.md`)

**Tier:** B.

## Global Constraints
- No order, auth, account or `.env` code in `src/bmm/scan/`. HTTP is GET only, and hosts must be in `config/scan_venues.toml [hosts]`.
- Refuse to run when `BMM_MODE=live`. Never import `ib_async` in `scan/`.
- Timestamps are int ns from `bmm.clock`. Never call `datetime.now()`.
- Do not guess venue facts. Anything unverified is `[?]` in `research/venue_facts.md` and has `verified = false` in toml.
- Fill rates count **contracts**, never trades.
- Money tests use `pytest.approx` with explicit tolerances. Tests: `uv run pytest -q -x 2>&1 | tail -20`. Never `cat` parquet.
- Turn cap: 25 for the whole goal.

## Review Focus
1. Venue returns 401/403 or non-JSON → `VenueNotPublic`, logged, and the other venues continue. *(Task 3)*
2. Empty or one-sided book → `D_side = 0`, `qualifying_min = max(venue_min, target_size)`, and the allocator does not crash. *(Tasks 3, 5)*
3. `n_trades_side = 0` on the first scans → the markout is fully the category mean (or 0 + `c_lower_bound`), and the report shows n. *(Tasks 5, 6)*
4. Venue cap or 15% market cap binds before the budget → the remainder flows elsewhere, never past $8k, and a block that doesn't fit is skipped whole. *(Task 5)*
5. Two-sided program where one side alone has negative EV → both sides get q together, or neither does. *(Task 5)*

---

### Task 1: Venue research (venue-researcher subagent)
**Files:** Modify `research/venue_facts.md`. Create `config/scan_venues.toml` and `tests/fixtures/scan/<venue>/*.json` (one raw unauthenticated GET response per endpoint).

- [ ] For each of Polymarket US, Kalshi and Gemini Predictions, record: host, program/incentive endpoint, book endpoint, trade endpoint (does it carry aggressor side?), whether auth is needed, and the reward period. Record the **exact scoring formula**: per-side or two-sided coupling, discount function, target size, max spread, venue min size, and whether the reward share is pro-rata of scores. Also record the **maker fee and maker rebate** per contract, collateral to rest a bid at p and an ask at p, the settlement source, and whether book levels expose participant/order identity. Every item gets a source URL, and unverified items are `[?]`.
- [ ] Kalshi: if the terms are webpage-only, set `program_source = "manual"` and `enabled = false`.
- [ ] ForecastEx: has a renewed incentive program been announced? Record yes/no/unknown, `checked = "2026-10-04"` and the source URL.
- [ ] Write `config/scan_venues.toml`, one `[venue.<id>]` per venue with these keys: `enabled, program_source, needs_auth, scoring ("pro_rata_quadratic"|"geometric_target"|...), two_sided, side_split, coll_bid, coll_ask, maker_fee, maker_rebate, *_source, *_verified`, plus `[hosts] allow = [...]` and `[forecastex] renewed_program, source, checked`.
- [ ] Commit: `research(scan): venue endpoints, scoring, fees, collateral`.

### Task 2: Skeleton, GET-only client, store, CLI, safety tests
**Files:** Create `src/bmm/scan/{__init__,models,http,store,config,cli}.py` and `tests/scan/test_scan_safety.py`. Modify `src/bmm/cli.py` (add a `scan` branch beside `spotcheck`), `pyproject.toml` (httpx) and `tests/spotcheck/test_safety.py` (parametrize).

**Produces:**
```python
Side = Literal["bid", "ask"]
@dataclass(frozen=True) class Program: venue: str; market_id: str; R_usd_hr: float; target_size: float | None; max_spread: float | None; venue_min: float; discount: float | None; scoring: str; two_sided: bool; maker_fee: float; maker_rebate: float; raw_hash: str
@dataclass(frozen=True) class Level: price: float; size: float; top_holder_share: float | None = None
@dataclass(frozen=True) class Book: bids: list[Level]; asks: list[Level]          # bids desc, asks asc
@dataclass(frozen=True) class Trade: ts_ns: int; price: float; size: float; aggressor: str | None   # "buy"|"sell"|None
@dataclass(frozen=True) class MarketMeta: market_id: str; title: str; category: str; expiry_ns: int | None; settle_source: str | None; settle_ns: int | None; reference_quality: str; in_game: bool
class VenueNotPublic(Exception): ...
class VenueAdapter(Protocol):
    name: str
    async def programs(self) -> list[Program]: ...
    async def meta(self, market_id: str) -> MarketMeta: ...
    async def book(self, market_id: str) -> Book: ...
    async def trades(self, market_id: str, since_ns: int) -> list[Trade]: ...
    def expected_reward(self, prog: Program, s: int, side: Side, book: Book) -> float: ...   # $/hr
    def qualifying_min(self, prog: Program, side: Side, book: Book) -> int: ...
    def s_star_closed_form(self, prog: Program, side: Side, book: Book, c: float) -> float | None: ...
    def scored_depth(self, prog: Program, side: Side, book: Book) -> tuple[float, float]: ...  # (D_side, depth_raw_side)
class GetOnlyClient:   # httpx.AsyncClient inside; no headers/auth params; 401/403 -> VenueNotPublic; host not allowed -> ValueError
    def __init__(self, allow_hosts: set[str]) -> None: ...
    async def get_json(self, url: str, params: dict[str, str] | None = None) -> Any: ...
def write(table: str, rows: list[dict[str, Any]], ts_ns: int, root: Path = Path("data/scan")) -> Path      # store.py
def read(table: str, date: str | None = None, root: Path = Path("data/scan")) -> pl.DataFrame               # store.py
def load(path: Path = Path("config/scan_venues.toml")) -> ScanConfig                                         # config.py
```
- [ ] **Step 1: Failing tests.** Replace `test_no_order_surface_in_spotcheck` with:
```python
HTTP_FORBIDDEN = re.compile(r"\.(post|put|patch|delete)\s*\(|/orders?\b(?!book)|Authorization|KALSHI-ACCESS|X-GEMINI-APIKEY|\.env\b")
PKGS = {"spotcheck": (SPOT, None), "scan": (ROOT / "src/bmm/scan", HTTP_FORBIDDEN)}

@pytest.mark.parametrize("pkg", sorted(PKGS))
def test_no_order_surface(pkg):
    path, extra = PKGS[pkg]
    files = sorted(path.rglob("*.py"))
    assert len(files) >= 5, f"vacuous walk for {pkg}"
    hits = scan(files)
    if extra:
        hits += [f"{f}:{i}" for f in files for i, l in enumerate(f.read_text().splitlines(), 1) if extra.search(l)]
    assert hits == []
```
`tests/scan/test_scan_safety.py`:
```python
@pytest.mark.parametrize("case", ["live_env", "ib_import", "foreign_host"])
def test_scan_port_env(case, monkeypatch):
    if case == "live_env":
        monkeypatch.setenv("BMM_MODE", "live")
        assert main(["scan", "rewards", "--once"]) != 0
    elif case == "ib_import":
        src = "\n".join(p.read_text() for p in Path("src/bmm/scan").rglob("*.py"))
        assert "ib_async" not in src and "4001" not in src
    else:
        with pytest.raises(ValueError):
            asyncio.run(GetOnlyClient({"api.example.com"}).get_json("https://evil.example.org/x"))
```
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. `scan/cli.py` returns 2 with a refusal message if `BMM_MODE == "live"`. **Step 4:** `make check` green. **Step 5:** Commit `feat(scan): skeleton, GET-only client, store, safety tests`.

### Task 3: Venue adapters with scoring
**Files:** Create `src/bmm/scan/venues/{__init__,pm_us,kalshi,gemini}.py` and `tests/scan/test_adapters.py`.
**Consumes:** Task 1 fixtures/toml and Task 2 types. **Produces:** `ADAPTERS: dict[str, Callable[[GetOnlyClient, VenueCfg], VenueAdapter]]`.

Rules:
- `R_usd_hr = period_amount / period_hours`, normalised in one place. Per-side reward uses the venue's `side_split` from the toml (`[?]` → 0.5 with `verified=false`).
- `qualifying_min = max(venue_min, ceil(target_size − depth_raw_side))`, floored at `venue_min`. Without a target size it is `venue_min`.
- **pm_us** (`pro_rata_quadratic`): we post at the touch, `w = ((v − d)/v)²` with d = distance from mid. `expected_reward = R_side·w·s/(w·s + D_side)`. `s_star_closed_form = (sqrt(R_side·w·D_side/c) − D_side)/w` when `D_side > 0` and `c > 0`, else None. This applies only if Task 1 confirmed pm_us is pro-rata. Otherwise all three return the generic numeric path (closed form None).
- **kalshi / gemini:** implement exactly the formula Task 1 recorded. `s_star_closed_form` returns None.
- Category keyword fallback: `metals: gold|silver|copper|platinum`, `energy: oil|wti|brent|natgas|gasoline`, `rates: fed|fomc|treasury|yield|sofr`, `fx: eur|usd|jpy|gbp|fx`, `equity_index: s&p|spx|nasdaq|ndx|dow|russell`, `crypto: btc|eth|bitcoin|ether|sol`, `sports`. `reference_quality`: `venue_ref` if the venue names a price source, `model_needed` for domain categories without one, else `none`.

- [ ] **Step 1: Failing tests:**
```python
@pytest.mark.parametrize("venue", ["pm_us", "kalshi", "gemini"])
def test_adapter_contract(venue, fake_client):
    cfg = load().venue[venue]
    if not cfg.enabled: pytest.skip(cfg.skip_reason)
    a = ADAPTERS[venue](fake_client(venue), cfg)
    progs = asyncio.run(a.programs()); p = progs[0]
    assert progs and all(x.R_usd_hr >= 0 for x in progs)
    b = asyncio.run(a.book(p.market_id))
    for side in ("bid", "ask"):
        r = [a.expected_reward(p, s, side, b) for s in range(0, 200, 10)]
        assert r[0] == 0 and all(x <= y + 1e-12 for x, y in zip(r, r[1:])) and r[-1] <= p.R_usd_hr + 1e-9
        assert a.qualifying_min(p, side, b) >= p.venue_min
    assert a.qualifying_min(p, "bid", Book([], [])) == max(p.venue_min, math.ceil(p.target_size or 0))

def test_pm_us_closed_form_matches_numeric(pm_us_adapter, pm_prog, pm_book):
    c = 0.002; a = pm_us_adapter
    cf = a.s_star_closed_form(pm_prog, "bid", pm_book, c)
    best = max(range(0, 5000), key=lambda s: a.expected_reward(pm_prog, s, "bid", pm_book) - c * s)
    assert cf == pytest.approx(best, abs=1.0)

def test_401_is_venue_not_public(fake_client):
    a = ADAPTERS["pm_us"](fake_client("pm_us", status=401), load().venue["pm_us"])
    with pytest.raises(VenueNotPublic): asyncio.run(a.programs())
```
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Pass. **Step 5:** Commit `feat(scan): venue adapters + scoring`.

### Task 4: Metrics, exclusions, snapshot loop
**Files:** Create `src/bmm/scan/metrics.py`, `src/bmm/scan/runner.py` and `tests/scan/test_metrics.py`.
**Produces:**
```python
def side_of(trade: Trade, mid: float | None) -> Side | None   # sell aggressor -> "bid"; buy -> "ask"; None -> tick rule vs mid (price<mid bid, >mid ask, ==mid None)
def fills_contracts_hr(trades: list[Trade], side: Side, mids: pl.DataFrame, window_ns: int) -> float   # sum(size)/hours
def markout_30m(trades: pl.DataFrame, mids: pl.DataFrame, side: Side) -> tuple[float | None, int]   # (size-weighted maker markout $/contract, n_trades)
def exclusion(meta: MarketMeta, book: Book, now_ns: int) -> str | None
async def snapshot_once(adapters: list[VenueAdapter], now_ns: int) -> dict[str, list[dict[str, Any]]]   # tables: snapshots, programs, trades, books
```
`books` stores up to 20 levels per side as list columns (`prices`, `sizes`, `holder_share`), so the report can rebuild `Book`.

- [ ] **Step 1: Failing tests:**
```python
def test_fill_rate_counts_contracts_not_trades():
    t = [Trade(0, .40, 7., "sell"), Trade(1, .40, 3., "sell"), Trade(2, .60, 5., "buy")]
    assert fills_contracts_hr(t, "bid", MIDS, 3600 * 10**9) == pytest.approx(10.0)
    assert fills_contracts_hr(t, "ask", MIDS, 3600 * 10**9) == pytest.approx(5.0)

def test_markout_maker_side_sign_and_size_weight():
    t = pl.DataFrame({"ts_ns": [0, 0], "price": [.40, .40], "size": [1., 3.], "aggressor": ["sell", "sell"]})
    m = pl.DataFrame({"ts_ns": [30 * 60 * 10**9], "mid": [.30]})
    val, n = markout_30m(t, m, "bid")        # maker bought at .40, mid -> .30: -0.10 each
    assert val == pytest.approx(-0.10) and n == 2

def test_markout_none_without_history():
    assert markout_30m(TRADES, pl.DataFrame({"ts_ns": [], "mid": []}), "bid") == (None, 0)

def test_unknown_aggressor_uses_tick_rule():
    assert side_of(Trade(0, .45, 1., None), .50) == "bid" and side_of(Trade(0, .50, 1., None), .50) is None

@pytest.mark.parametrize("meta,book,reason", [
    (META_INGAME, BOOK, "live in-game"),
    (META_CRYPTO_12H, BOOK, "crypto <1d to expiry"),
    (META_OK, BOOK_ONE_HOLDER_60PCT, "one participant >50% at touch"),
    (META_OK, BOOK_NO_HOLDER_INFO, None),
])
def test_exclusion(meta, book, reason): assert exclusion(meta, book, NOW) == reason
```
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. Markout uses an as-of join of mids at `ts+30m` with ≤ 5 min tolerance. Bid side: `mid_later − price`. Ask side: `price − mid_later`. `runner.py` loops on `--interval`, writes all four tables, logs `VenueNotPublic` per venue and continues. **Step 4:** Pass. **Step 5:** Commit `feat(scan): metrics, exclusions, snapshot loop`.

### Task 5: Cost, shrinkage, allocator (one quant-reviewer pass)
**Files:** Create `src/bmm/scan/allocator.py` and `tests/scan/test_allocator.py`.
**Produces:**
```python
@dataclass(frozen=True)
class Half:
    key: str; market: str; venue: str; two_sided: bool
    reward: Callable[[int], float]       # $/hr for s contracts (adapter.expected_reward bound to prog/side/book)
    c: float; coll: float; q: int
    s_star_cf: float | None = None; c_lower_bound: bool = False
@dataclass(frozen=True)
class Alloc: key: str; s: int; reward_day: float; cost_day: float; collateral: float; roc_day: float; c_lower_bound: bool
def shrink_markout(m_mkt: float | None, n: int, m_cat: float | None, n0: int = 20) -> tuple[float, bool]   # (m, lower_bound_flag)
def cost_hr(fills_side_hr: float, depth_raw_side: float, m: float, maker_fee: float, maker_rebate: float, coll: float) -> float
def ev(h: Half, s: int) -> float
def s_star(h: Half, s_cap: int) -> int
def allocate(halves: list[Half], budget: float = 8000.0, mkt_cap: float = 0.15, venue_cap: float = 0.40) -> list[Alloc]
```
- [ ] **Step 1: Failing tests:**
```python
def pr(R, D): return lambda s: R * s / (s + D) if s > 0 else 0.0
def H(**kw):
    d = dict(key="a:bid", market="a", venue="v", two_sided=False, reward=pr(10., 100.), c=0.001, coll=0.5, q=1)
    return Half(**(d | kw))

def test_cost_per_fill_floor_and_fees():
    # m=-0.03 adverse, rebate .01, fee .002 -> cost/fill .022; f = 50/100
    assert cost_hr(50., 100., -0.03, 0.002, 0.01, 0.6) == pytest.approx(0.5 * 0.022 + 0.6 * 0.05 / 8760, rel=1e-12)
    # favourable markout + big rebate -> cost/fill floored at 0
    assert cost_hr(50., 100., +0.05, 0.0, 0.02, 0.6) == pytest.approx(0.6 * 0.05 / 8760, rel=1e-12)

def test_shrink_markout():
    assert shrink_markout(-0.04, 25, -0.01) == (pytest.approx(-0.04), False)
    assert shrink_markout(-0.04, 5, -0.01) == (pytest.approx(0.25 * -0.04 + 0.75 * -0.01), False)
    assert shrink_markout(None, 0, -0.01) == (pytest.approx(-0.01), False)
    assert shrink_markout(None, 0, None) == (0.0, True)

def test_numeric_s_star_matches_bruteforce():
    h = H(reward=pr(10., 100.), c=0.002)
    assert s_star(h, 5000) == max(range(0, 5001), key=lambda s: ev(h, s))

def test_qualifying_raise_or_zero():
    assert s_star(H(reward=pr(10., 100.), c=0.002, q=1000), 5000) == 1000           # EV(1000) > 0 -> raise
    assert s_star(H(reward=pr(10., 100.), c=0.05, q=1000), 5000) == 0               # EV(1000) < 0 -> zero

def test_closed_form_used_and_rounded_to_best_integer():
    h = H(s_star_cf=123.4, reward=pr(10., 100.), c=0.002)
    assert s_star(h, 5000) in (123, 124)

def test_caps_and_budget():
    hs = [H(key=f"m{i}:bid", market=f"m{i}", venue="v1" if i < 5 else "v2", reward=pr(1000., 10.), c=1e-6, coll=1.) for i in range(10)]
    out = {a.key: a for a in allocate(hs)}
    assert sum(a.collateral for a in out.values()) <= 8000 + 1e-9
    assert max(a.collateral for a in out.values()) <= 1200 + 1e-9
    for v in ("v1", "v2"):
        assert sum(out[h.key].collateral for h in hs if h.venue == v) <= 3200 + 1e-9

def test_greedy_matches_bruteforce_unit_blocks():
    a, b = H(key="a:bid", market="a", reward=pr(5., 20.)), H(key="b:bid", market="b", venue="w", reward=pr(8., 50.))
    out = {x.key: x.s for x in allocate([a, b], budget=30, mkt_cap=1, venue_cap=1)}
    tot = lambda i, j: ev(a, i) + ev(b, j)
    best = max(((i, j) for i in range(61) for j in range(61) if 0.5 * (i + j) <= 30), key=lambda t: tot(*t))
    assert tot(out["a:bid"], out["b:bid"]) == pytest.approx(tot(*best), abs=1e-9)

def test_two_sided_block_all_or_nothing():
    bid = H(key="m:bid", market="m", two_sided=True, reward=pr(10., 5.), q=20)
    ask = H(key="m:ask", market="m", two_sided=True, reward=pr(0.01, 5.), q=20)   # ask alone EV<0, pair EV>0
    out = {x.key: x.s for x in allocate([bid, ask])}
    assert out["m:bid"] >= 20 and out["m:ask"] >= 20
    out = {x.key: x.s for x in allocate([bid, ask], budget=15)}                     # block (40*0.5=20) doesn't fit
    assert out == {"m:bid": 0, "m:ask": 0}

def test_lower_bound_flag_passthrough():
    (a,) = allocate([H(c_lower_bound=True)]); assert a.c_lower_bound
```
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement:
```python
HOURS_YR, COLL_RATE, N0, EPS = 8760.0, 0.05, 20, 1e-9

def shrink_markout(m_mkt, n, m_cat, n0=N0):
    if m_cat is None:
        return (m_mkt, False) if m_mkt is not None else (0.0, True)
    if m_mkt is None or n <= 0:
        return m_cat, False
    w = min(n, n0) / n0
    return w * m_mkt + (1 - w) * m_cat, False

def cost_hr(fills_side_hr, depth_raw_side, m, maker_fee, maker_rebate, coll):
    f = fills_side_hr / max(depth_raw_side, 1.0)
    per_fill = max(0.0, max(0.0, -m) - maker_rebate + maker_fee)
    return f * per_fill + coll * COLL_RATE / HOURS_YR

def ev(h, s):
    return h.reward(s) - h.c * s if s > 0 else 0.0

def _argmax_int(f, lo, hi):          # assumes f unimodal on [lo, hi]
    while hi - lo > 2:
        m1, m2 = lo + (hi - lo) // 3, hi - (hi - lo) // 3
        if f(m1) < f(m2): lo = m1 + 1
        else: hi = m2
    return max(range(lo, hi + 1), key=f)

def s_star(h, s_cap):
    if h.q > s_cap: return 0
    if h.s_star_cf is not None:
        lo = max(0, min(s_cap, math.floor(h.s_star_cf)))
        s = max((lo, min(s_cap, lo + 1)), key=lambda x: ev(h, x))
    else:
        s = _argmax_int(lambda x: ev(h, x), h.q, s_cap)
    s = max(s, h.q)                    # below-threshold optimum is raised to q ...
    return s if ev(h, s) > 0 else 0    # ... unless 0 has higher EV

def allocate(halves, budget=8000.0, mkt_cap=0.15, venue_cap=0.40):
    by = {h.key: h for h in halves}
    s = {h.key: 0 for h in halves}
    groups: dict[str, list[Half]] = {}
    for h in halves: groups.setdefault(h.market if h.two_sided else h.key, []).append(h)
    smax = {}
    for h in halves:
        st = s_star(h, int(mkt_cap * budget // h.coll))
        smax[h.key] = max(st, h.q) if h.two_sided else st
    used, vused, mused = 0.0, defaultdict(float), defaultdict(float)
    heap, seq = [], itertools.count()
    for g, hs in groups.items():
        if not hs[0].two_sided and smax[hs[0].key] == 0: continue
        bev = sum(ev(h, h.q) for h in hs); bcoll = sum(h.q * h.coll for h in hs)
        if bev > 0: heapq.heappush(heap, (-bev / bcoll, next(seq), g, None))
    while heap:
        negr, _, g, hk = heapq.heappop(heap)
        if -negr <= 0: break
        hs = groups[g]; mkt, v = hs[0].market, hs[0].venue
        add = {h.key: h.q for h in hs} if hk is None else {hk: 1}
        if hk is not None and s[hk] + 1 > smax[hk]: continue
        dc = sum(n * by[k].coll for k, n in add.items())
        if used + dc > budget + EPS or vused[v] + dc > venue_cap * budget + EPS or mused[mkt] + dc > mkt_cap * budget + EPS:
            continue
        for k, n in add.items(): s[k] += n
        used += dc; vused[v] += dc; mused[mkt] += dc
        for k in add:
            h = by[k]
            if s[k] < smax[k]:
                heapq.heappush(heap, (-(ev(h, s[k] + 1) - ev(h, s[k])) / h.coll, next(seq), g, k))
    return [_alloc(h, s[h.key]) for h in halves]

def _alloc(h, n):
    rd, cd, col = 24 * h.reward(n) if n else 0.0, 24 * h.c * n, n * h.coll
    return Alloc(h.key, n, rd, cd, col, (rd - cd) / col if col else 0.0, h.c_lower_bound)
```
- [ ] **Step 4:** Pass.
- [ ] **Step 5: quant-reviewer** (one pass), given `git diff -- src/bmm/scan/allocator.py tests/scan/test_allocator.py` + `docs/spec/60-rewards-scanner.md`. Ask it to check:
  - units ($/hr/contract) and the fee/rebate sign
  - the shrinkage weight
  - the queue-share fill approximation
  - the qualifying raise-or-zero rule
  - pair-block semantics
  - the unimodality assumption of the integer ternary search
  - greedy behaviour with lumpy blocks

  Fix the findings in one round.
- [ ] **Step 6:** Commit `feat(scan): cost, shrinkage, allocator`.

### Task 6: Report, program diff, end-to-end run
**Files:** Create `src/bmm/scan/report.py` and `tests/scan/test_report.py`.
**Consumes:** `store.read`, `ADAPTERS` (scoring only, no client calls), `shrink_markout`, `cost_hr`, `allocate`, `config.load`.
**Produces:** `build_report(date: str, root: Path = Path("data/scan")) -> str` and `program_changes(cur: pl.DataFrame, prev: pl.DataFrame | None) -> pl.DataFrame` (venue, market_id, change ∈ {new, changed, ended}).

Steps inside `build_report`:
1. Take the latest snapshot and book per half-market.
2. Compute `m_cat` as the size-weighted mean markout per category, then `shrink_markout`, then `cost_hr`.
3. Build each `Half` with `reward = partial(adapter.expected_reward, prog, side=side, book=book)`, `q = adapter.qualifying_min(...)` and `s_star_cf = adapter.s_star_closed_form(..., c)`.
4. Run `allocate`.
5. Render the 6 sections. Top 25 groups the two sides by market and shows `n_bid/n_ask`. It drops excluded markets and markets with `c_lower_bound`. Those still appear in a "pending markouts" list.

- [ ] **Step 1: Failing tests** (synthetic parquet in `tmp_path` via `store.write`, adapters stubbed):
```python
def test_report_has_six_sections(tmp_path):
    seed(tmp_path); md = build_report("2026-10-04", tmp_path)
    for h in ["## 1. Top 25", "## 2. Venue / category", "## 3. Domain sleeve candidates", "## 4. Program changes", "## 5. Exclusions", "## 6. Venue rules"]:
        assert h in md

def test_top25_shows_n_per_side(tmp_path):
    seed(tmp_path); top = build_report("2026-10-04", tmp_path).split("## 1.")[1].split("## 2.")[0]
    assert "n_bid" in top and "n_ask" in top

def test_domain_sleeve_not_dropped_for_missing_reference(tmp_path):
    seed(tmp_path, extra=[{"category": "metals", "reference_quality": "model_needed", "market_id": "GOLD-X"}])
    assert "GOLD-X" in build_report("2026-10-04", tmp_path).split("## 3.")[1].split("## 4.")[0]

def test_first_scan_program_diff():
    cur = pl.DataFrame({"venue": ["pm_us"], "market_id": ["a"], "raw_hash": ["h"]})
    assert program_changes(cur, None)["change"].to_list() == ["new"]

def test_program_changed_and_ended():
    prev = pl.DataFrame({"venue": ["v", "v"], "market_id": ["a", "b"], "raw_hash": ["h1", "h"]})
    cur = pl.DataFrame({"venue": ["v"], "market_id": ["a"], "raw_hash": ["h2"]})
    assert sorted(program_changes(cur, prev)["change"].to_list()) == ["changed", "ended"]
```
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. Write the report to `reports/rewards_scan_<date>.md`. **Step 4:** `make check` green.
- [ ] **Step 5: Evidence** (real public GETs, no auth):
```bash
uv run bmm scan rewards --once && uv run bmm scan report --date 2026-10-04
head -40 reports/rewards_scan_2026-10-04.md
```
Paste the output. Skipped venues are logged with reasons. On a single scan, markouts fall back to category means or `c_lower_bound`.
- [ ] **Step 6:** Commit `feat(scan): rewards report`.
