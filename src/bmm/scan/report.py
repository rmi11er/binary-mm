"""Report builder: reads the parquet tables, rebuilds each half-market and allocates (spec 60).

Does no network I/O. Venue scoring comes from the adapters' pure methods, so the report is
reproducible from `data/scan/` alone.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import polars as pl

from bmm.clock import now_ns
from bmm.scan import config as cfgmod
from bmm.scan import store
from bmm.scan.allocator import N0, Alloc, Half, allocate, cost_hr, ev, s_star, shrink_markout
from bmm.scan.config import ScanConfig
from bmm.scan.metrics import adverse_jump, calibrate_lambda
from bmm.scan.models import Book, Level, Program, VenueAdapter
from bmm.scan.quality import cadence_flags, cadence_histogram, d_churn, stale_stats, touch_changes
from bmm.scan.schedule import is_open_ended, pool_remaining, reward_hours

DOMAIN_CATS = {"metals", "energy", "rates", "fx", "equity_index"}
BUDGET = 8000.0
MKT_CAP = 0.15
TOP_N = 25
STRESS_MKT = 0.02  # of bankroll, per market
STRESS_TOTAL = 0.10


def program_changes(cur: pl.DataFrame, prev: pl.DataFrame | None) -> pl.DataFrame:
    """new / changed / ended programs between two scans (keyed on venue, market_id, raw_hash)."""
    out: list[dict[str, str]] = []
    c = {(r["venue"], r["market_id"]): r["raw_hash"] for r in cur.iter_rows(named=True)}
    p = (
        {(r["venue"], r["market_id"]): r["raw_hash"] for r in prev.iter_rows(named=True)}
        if prev is not None and not prev.is_empty()
        else None
    )
    for k, h in c.items():
        if p is None or k not in p:
            out.append({"venue": k[0], "market_id": k[1], "change": "new"})
        elif p[k] != h:
            out.append({"venue": k[0], "market_id": k[1], "change": "changed"})
    for k in (p or {}).keys() - c.keys():
        out.append({"venue": k[0], "market_id": k[1], "change": "ended"})
    return pl.DataFrame(
        out, schema={"venue": pl.String, "market_id": pl.String, "change": pl.String}
    )


def _latest(df: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    return df.sort("ts_ns").group_by(keys, maintain_order=True).last()


def _book(row: Mapping[str, Any]) -> Book:
    def lv(side: str) -> list[Level]:
        return [
            Level(p, s, h)
            for p, s, h in zip(
                row[f"{side}_prices"], row[f"{side}_sizes"], row[f"{side}_holder"], strict=True
            )
        ]

    return Book(lv("bid"), lv("ask"))


def _cat_means(snaps: pl.DataFrame) -> tuple[dict[str, float], float | None]:
    """Markout weighted by number of markout samples: per category and overall."""
    num: defaultdict[str, float] = defaultdict(float)
    den: defaultdict[str, float] = defaultdict(float)
    for r in snaps.iter_rows(named=True):
        m, n = r["markout_30m_side"], r["n_markout_side"] or 0
        if m is not None and n > 0:
            num[r["category"]] += m * n
            den[r["category"]] += n
    cats = {c: num[c] / den[c] for c in den}
    tot = sum(den.values())
    return cats, (sum(num.values()) / tot if tot else None)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    def f(x: Any) -> str:
        if isinstance(x, float):
            return "" if math.isnan(x) else f"{x:,.4g}" if abs(x) < 1 else f"{x:,.2f}"
        return "" if x is None else str(x)

    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(f(x) for x in r) + " |" for r in rows]
    return "\n".join(lines)


def _load_conf() -> ScanConfig:
    try:
        return cfgmod.load()
    except FileNotFoundError:
        return ScanConfig(frozenset(), {}, {})


def _default_adapters(conf: ScanConfig) -> dict[str, VenueAdapter]:
    from bmm.scan.venues import ADAPTERS

    # Scoring methods are pure; no client is needed (and none may be used) here.
    return {n: ADAPTERS[n](None, c) for n, c in conf.venue.items() if n in ADAPTERS}


MAX_ROC_DAY = 1.0  # a half-market returning more than 100% of collateral per day is a defect


@dataclass
class Analysis:
    date: str
    allocs: dict[str, Alloc] = field(default_factory=dict)
    info: dict[str, dict[str, Any]] = field(default_factory=dict)
    halves: list[tuple[Half, dict[str, Any]]] = field(default_factory=list)
    markout: dict[str, tuple[float, str]] = field(default_factory=dict)  # key -> (m, source)
    flags: dict[str, list[str]] = field(default_factory=dict)
    stress_per: dict[str, float] = field(default_factory=dict)
    fills: dict[str, float] = field(default_factory=dict)  # key -> fills/hr/side actually used
    pools: dict[str, float] = field(default_factory=dict)  # key -> pool remaining $
    lam: float | None = None
    excluded: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[tuple[str, str, str]] = field(default_factory=list)  # venue, market, reason
    defects: list[dict[str, Any]] = field(default_factory=list)
    review: list[dict[str, Any]] = field(
        default_factory=list
    )  # needs_review_recurring (not DEFECT)
    stats: dict[str, dict[str, float]] = field(
        default_factory=dict
    )  # standalone s0 economics per half
    period_h: dict[str, float | None] = field(default_factory=dict)
    stream: dict[str, int] = field(
        default_factory=dict
    )  # windows in the recurring stream (0 = not a stream)
    pool_win: dict[str, float] = field(default_factory=dict)
    hours24: dict[str, float] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)
    over_at_alloc: list[dict[str, Any]] = field(
        default_factory=list
    )  # RoC > 100%/day at the allocated size
    eligible: set[str] = field(default_factory=set)  # half keys that entered allocation
    unknown_conc: int = 0
    standalone: dict[str, float] = field(default_factory=dict)  # "venue:market" -> net $/day at s*
    snaps: pl.DataFrame = field(default_factory=pl.DataFrame)


def _blocked_reason(conf: ScanConfig, venue: str) -> str | None:
    v = conf.venue.get(venue)
    if v is not None and v.raw.get("pool_scope_verified") is False:
        return "pool scope unverified (config/scan_venues.toml: pool_scope_verified = false)"
    return None


def _cap(h: Half) -> int:
    cap = math.floor(MKT_CAP * BUDGET / h.coll + 1e-9) if h.coll > 0 else 0
    if h.stress_per > 0:
        cap = min(cap, math.floor(STRESS_MKT * BUDGET / h.stress_per + 1e-9))
    return cap


def analyze(
    date: str,
    root: Path = store.DEFAULT_ROOT,
    adapters: Mapping[str, Any] | None = None,
    conf: ScanConfig | None = None,
    shares_factor: float = 1.0,
) -> Analysis | None:
    """`shares_factor` scales fills estimated from sharesTraded: 1.0 = every contract counted once
    (total/2 per side); 0.5 = both legs of a trade counted (total/4 per side)."""
    snaps_all = store.read("snapshots", date, root)
    if snaps_all.is_empty():
        return None
    conf = conf or _load_conf()
    adapters = adapters if adapters is not None else _default_adapters(conf)
    snaps = _latest(snaps_all, ["venue", "market_id", "side"])
    books = {
        (r["venue"], r["market_id"]): _book(r)
        for r in _latest(store.read("books", date, root), ["venue", "market_id"]).iter_rows(
            named=True
        )
    }
    progs: dict[tuple[str, str], Program] = {}
    for r in _latest(store.read("programs", date, root), ["venue", "market_id"]).iter_rows(
        named=True
    ):
        progs[(r["venue"], r["market_id"])] = Program(
            **{k: r[k] for k in Program.__dataclass_fields__ if k in r}
        )

    an = Analysis(date, snaps=snaps)
    cat_mean, overall = _cat_means(snaps)
    rows = list(snaps.iter_rows(named=True))
    # lambda only from strong (non-weak) proxies: candle / price-history mids
    an.lam = calibrate_lambda(
        [
            (r["n_markout_proxied_side"], r["markout_proxied_side"], r["vol30_proxy"])
            for r in rows
            if (r.get("n_markout_proxied_side") or 0) > 0
            and r.get("markout_proxied_side") is not None
            and r.get("vol30_proxy") is not None
            and not r.get("weak_proxy")
        ]
    )
    no_reward: set[tuple[str, str]] = set()
    for r in rows:
        k = (r["venue"], r["market_id"])
        if r["excluded"]:
            an.excluded.append(r)
            continue
        if r["side"] == "bid" and r["top_holder_share"] is None:
            an.unknown_conc += 1
        ad, prog, book = adapters.get(r["venue"]), progs.get(k), books.get(k)
        if ad is None or prog is None or book is None:
            continue
        ts = int(r["ts_ns"])
        hours24 = reward_hours(prog, ts)
        if hours24 <= 0:
            if k not in no_reward:
                no_reward.add(k)
                an.blocked.append(
                    (r["venue"], r["market_id"], "no scheduled reward in the next 24h")
                )
            continue
        side, cat = r["side"], r["category"]
        org_m, org_n = r["markout_30m_side"], r["n_markout_side"] or 0
        px_m, px_n = r.get("markout_proxied_side"), r.get("n_markout_proxied_side") or 0
        vol = r.get("vol30_proxy")
        if px_m is not None and px_n > 0:
            prior, psrc = px_m, "proxied"
        elif vol is not None and an.lam is not None:
            prior, psrc = -an.lam * vol, "vol-proxy"
        else:
            prior = cat_mean.get(cat, overall)
            psrc = "category" if prior is not None else None
        m, lb = shrink_markout(org_m, org_n, prior)
        w = min(org_n, N0) / N0 if org_m is not None else 0.0
        src = "organic" if w >= 1 else (f"blend:{psrc}" if w > 0 and psrc else psrc or "none")
        shares = r.get("fills_shares_hr")
        fills = shares / 2 * shares_factor if shares is not None else r["fills_side_hr"]
        c = cost_hr(
            fills, r["depth_raw_side"], m, r["maker_fee"], r["maker_rebate"], r["coll_side"]
        )
        px = r.get("bid") if side == "bid" else r.get("ask")
        if px is None:
            px = r.get("mid") or 0.5
        sp = adverse_jump(side, px, r.get("jump_class") or "normal", r.get("jump_p99_1h"))
        key = f"{r['venue']}:{r['market_id']}:{side}"
        h = Half(
            key=key,
            market=f"{r['venue']}:{r['market_id']}",
            venue=r["venue"],
            two_sided=prog.two_sided,
            reward=lambda s, ad=ad, prog=prog, side=side, book=book: ad.expected_reward(
                prog, s, side, book
            ),
            c=c,
            coll=r["coll_side"],
            q=int(ad.qualifying_min(prog, side, book)),
            s_star_cf=ad.s_star_closed_form(prog, side, book, c),
            c_lower_bound=lb or bool(r.get("fills_lower_bound")),
            stress_per=sp,
            hours24=hours24,
        )
        an.halves.append((h, r))
        an.info[key], an.markout[key], an.stress_per[key] = r, (m, src), sp
        an.fills[key], an.pools[key] = fills, pool_remaining(prog, ts)
        an.stream[key], an.hours24[key], an.pool_win[key] = (
            prog.stream_windows,
            hours24,
            prog.pool_usd,
        )
        an.period_h[key] = (
            (prog.end_ns - prog.start_ns) / 3.6e12 if prog.end_ns and prog.start_ns else None
        )
        fl = []
        if src.startswith(("proxied", "vol-proxy", "blend:proxied", "blend:vol-proxy")):
            fl.append("mk:proxied")
            if r.get("weak_proxy"):
                fl.append("weak_proxy")
        if r.get("fills_proxied"):
            fl.append("fills:proxied")
        if is_open_ended(prog):
            fl.append("open_end")
        if prog.schedule in ("", "[]"):
            fl.append("no_schedule")
        an.flags[key] = fl

    blocked_markets: set[tuple[str, str]] = set()
    pre: list[tuple[Half, dict[str, Any]]] = []
    for h, r in an.halves:
        why = _blocked_reason(conf, h.venue)
        if why and (h.venue, r["market_id"]) not in blocked_markets:
            blocked_markets.add((h.venue, r["market_id"]))
            an.blocked.append((h.venue, r["market_id"], why))
        elif not why:
            pre.append((h, r))

    # --- sanity guard: RoC > 100%/day or reward above the pool still to be paid = DEFECT ---
    stats: dict[str, dict[str, float]] = {}
    mkt_reward: defaultdict[str, float] = defaultdict(float)
    guarded = [
        (h, r) for h, r in an.halves if not h.c_lower_bound
    ]  # lower-bound cost overstates RoC
    for h, _ in guarded:
        s0 = s_star(h, _cap(h))
        rd, cd = h.hours24 * h.reward(s0), h.hours24 * h.c * s0
        col = (
            s0 * h.coll
        )  # return ON COLLATERAL, as specified (tail-price quotes with ~0 collateral are flagged)
        roc = (rd - cd) / col if col > 0 else 0.0
        stats[h.key] = {"s0": s0, "rd": rd, "cd": cd, "col": col, "roc": roc,
                        "roc_active": roc * max(1.0, 24.0 / h.hours24)}  # fmt: skip
        mkt_reward[h.market] += rd
    defect_keys: set[str] = set()
    an.stats = stats
    for h, r in guarded:
        st, pool = stats[h.key], an.pools[h.key]
        structural: list[str] = []
        roc_reasons: list[str] = []
        if h.coll <= 0:
            structural.append("collateral per contract <= 0")
        if st["roc_active"] > MAX_ROC_DAY:
            roc_reasons.append(
                f"RoC {st['roc_active']:.0%}/day (per active hour, scaled to 24h) > {MAX_ROC_DAY:.0%}"
            )
        if mkt_reward[h.market] > pool * (1 + 1e-6) + 1e-9:
            structural.append(
                f"reward ${mkt_reward[h.market]:,.2f}/day > pool remaining ${pool:,.2f}"
            )
        if not (structural or roc_reasons):
            continue
        n_win = an.stream[h.key]
        if n_win >= 3 and roc_reasons and not structural:
            bucket = an.review
            reasons = [
                (
                    f"RoC {st['roc_active']:.0%}/day on per-window collateral > {MAX_ROC_DAY:.0%} "
                    f"(recurring stream of {n_win} windows: $/day is the sum of scheduled pools, collateral is reused)"
                )
            ]
        else:
            bucket, reasons = an.defects, structural + roc_reasons
        defect_keys.add(h.key)
        ph = an.period_h.get(h.key)
        win24 = h.hours24 / ph if ph else None
        net = st["rd"] - st["cd"]
        bucket.append(
            {"market": h.market, "key": h.key, "side": r["side"], "reasons": reasons,
             "inputs": {"R_side": r.get("R_side"), "hours24": h.hours24, "pool_remaining": pool,
                        "D_side": r.get("D_side"), "c": h.c, "coll": h.coll, "s0": st["s0"],
                        "reward_day": st["rd"], "cost_day": st["cd"], "roc": st["roc_active"],
                        "markout": an.markout[h.key], "fills": an.fills[h.key],
                        "flags": " ".join(an.flags[h.key]), "windows": n_win,
                        "pool_window": an.pool_win[h.key], "windows_24h": win24,
                        "roc_window": net / win24 / st["col"] if win24 and st["col"] > 0 else None}}
        )  # fmt: skip
    eligible = [(h, r) for h, r in pre if h.key not in defect_keys]
    an.eligible = {h.key for h, _ in eligible}
    an.allocs = {
        a.key: a
        for a in allocate(
            [h for h, _ in eligible if not h.c_lower_bound], BUDGET, MKT_CAP,
            stress_mkt_cap=STRESS_MKT, stress_total_cap=STRESS_TOTAL, bankroll=BUDGET,
        )
    }  # fmt: skip
    by_key = {h.key: h for h, _ in eligible}
    an.over_at_alloc = [
        {
            "market": by_key[x.key].market,
            "side": an.info[x.key]["side"],
            "s": x.s,
            "collateral": x.collateral,
            "roc": x.roc_day,
            "hours24": by_key[x.key].hours24,
        }
        for x in an.allocs.values()
        if x.s > 0 and x.roc_day > MAX_ROC_DAY
    ]
    for (
        h,
        _,
    ) in eligible:  # standalone potential (also for pending markets): drives the fast-tier ranking
        an.standalone[h.market] = an.standalone.get(h.market, 0.0) + h.hours24 * ev(
            h, s_star(h, _cap(h))
        )
    review_keys, defect_k, pre_keys = (
        {d["key"] for d in an.review},
        {d["key"] for d in an.defects},
        {h.key for h, _ in pre},
    )
    for h, _ in an.halves:
        k = h.key
        a_ = an.allocs.get(k)
        an.status[k] = (
            "needs_review_recurring" if k in review_keys
            else "DEFECT" if k in defect_k
            else "not_allocated" if k not in pre_keys
            else "pending_markouts" if h.c_lower_bound
            else "allocated" if a_ is not None and a_.s > 0
            else "not_funded"
        )  # fmt: skip
    return an


def rank_markets(an: Analysis) -> list[tuple[str, float]]:
    """Funded markets, best net return on collateral per day first (both sides combined)."""
    g: defaultdict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for k, x in an.allocs.items():
        if x.s > 0:
            m = an.info[k]
            g[f"{m['venue']}:{m['market_id']}"][0] += x.reward_day - x.cost_day
            g[f"{m['venue']}:{m['market_id']}"][1] += x.collateral
    return sorted(((m, v[0] / v[1]) for m, v in g.items()), key=lambda kv: (-kv[1], kv[0]))


def sensitivity(
    date: str,
    root: Path = store.DEFAULT_ROOT,
    adapters: Mapping[str, Any] | None = None,
    conf: ScanConfig | None = None,
    n: int = 10,
) -> dict[str, Any]:
    """Rank under both sharesTraded counting assumptions and say whether the top n changes."""
    a, b = analyze(date, root, adapters, conf, 1.0), analyze(date, root, adapters, conf, 0.5)
    ra = rank_markets(a) if a else []
    rb = rank_markets(b) if b else []
    top_a, top_b = [m for m, _ in ra[:n]], [m for m, _ in rb[:n]]
    pos_a, pos_b = (
        {m: i + 1 for i, (m, _) in enumerate(ra)},
        {m: i + 1 for i, (m, _) in enumerate(rb)},
    )
    moved = [
        {"market": m, "rank_a": pos_a.get(m), "rank_b": pos_b.get(m)}
        for m in dict.fromkeys(top_a + top_b)
        if pos_a.get(m) != pos_b.get(m)
    ]
    return {"top_a": top_a, "top_b": top_b, "changed": top_a != top_b, "moved": moved,
            "entered": [m for m in top_b if m not in top_a], "left": [m for m in top_a if m not in top_b]}  # fmt: skip


def refresh_ranking(
    date: str,
    root: Path = store.DEFAULT_ROOT,
    adapters: Mapping[str, Any] | None = None,
    conf: ScanConfig | None = None,
) -> None:
    """Write the candidate ranking (standalone net $/day, best first) the fast tier reads."""
    an = analyze(date, root, adapters, conf)
    if an is None or not an.standalone:
        return
    ts = now_ns()
    ranked = sorted(an.standalone.items(), key=lambda kv: (-kv[1], kv[0]))
    store.write(
        "ranking",
        [
            {
                "ts_ns": ts,
                "venue": m.split(":", 1)[0],
                "market_id": m.split(":", 1)[1],
                "rank": i,
                "net_day": v,
            }
            for i, (m, v) in enumerate(ranked, 1)
        ],
        ts,
        root,
    )


def top_candidates(
    root: Path = store.DEFAULT_ROOT, n: int = 40, date: str | None = None
) -> set[tuple[str, str]]:
    r = store.read("ranking", None, root)
    if r.is_empty():
        return set()
    r = r.filter(pl.col("ts_ns") == r["ts_ns"].max()).sort("rank").head(n)
    return {(v, m) for v, m in r.select("venue", "market_id").iter_rows()}


def build_report(
    date: str,
    root: Path = store.DEFAULT_ROOT,
    adapters: Mapping[str, Any] | None = None,
    conf: ScanConfig | None = None,
) -> str:
    head = f"# Rewards scan {date}\n"
    an = analyze(date, root, adapters, conf)
    if an is None:
        return head + f"\nNo snapshots for {date}.\n"
    conf = conf or _load_conf()
    halves, allocs, info = an.halves, an.allocs, an.info
    eligible_keys = an.eligible

    # 1. top markets (both sides grouped)
    agg: dict[str, dict[str, Any]] = {}
    for key, a in allocs.items():
        if a.s <= 0:
            continue
        r = info[key]
        g = agg.setdefault(
            f"{r['venue']}:{r['market_id']}",
            {
                "r": r,
                "s_bid": 0,
                "s_ask": 0,
                "st_bid": 0.0,
                "st_ask": 0.0,
                "rd": 0.0,
                "cd": 0.0,
                "col": 0.0,
                "n_bid": 0,
                "n_ask": 0,
                "flags": set(),
            },
        )
        sd = r["side"]
        g["s_" + sd] += a.s
        g["st_" + sd] += a.stress_loss
        g["n_" + sd] = r["n_markout_side"] or 0
        g["rd"] += a.reward_day
        g["cd"] += a.cost_day
        g["col"] += a.collateral
        g["flags"].update(an.flags[key])
    ranked = sorted(
        agg.items(), key=lambda kv: (kv[1]["rd"] - kv[1]["cd"]) / kv[1]["col"], reverse=True
    )
    top_rows = [
        [i, k, g["r"]["category"], g["r"].get("jump_class") or "normal", g["s_bid"], g["s_ask"], g["rd"],
         g["cd"], g["col"], (g["rd"] - g["cd"]) / g["col"], g["st_bid"], g["st_ask"],
         g["st_bid"] + g["st_ask"], g["n_bid"], g["n_ask"], " ".join(sorted(g["flags"]))]
        for i, (k, g) in enumerate(ranked[:TOP_N], 1)
    ]  # fmt: skip
    pending = sorted({h.market for h, _ in halves if h.c_lower_bound and h.key in eligible_keys})
    tot_stress = sum(a.stress_loss for a in allocs.values())
    lam = f"{an.lam:.3f}" if an.lam is not None else "n/a"
    s1 = (
        f"## 1. Top 25 markets\n\nBudget ${BUDGET:,.0f}; market cap {MKT_CAP:.0%}; venue cap 40%. "
        f"Stress loss = size x adverse jump (high jump class: full distance to 0/1; else empirical 99th "
        f"pct 1h move): caps {STRESS_MKT:.0%} per market, {STRESS_TOTAL:.0%} total of bankroll; "
        f"allocated ${tot_stress:,.0f} ({tot_stress / BUDGET:.1%}). Net of fill cost. n_bid / n_ask = "
        f"fills with an organic 30m markout. `proxied` flags backfilled / derived inputs "
        f"(markout lambda = {lam} calibrated on Kalshi).\n\n"
        + _table(
            [
                "#",
                "market",
                "category",
                "jump",
                "s_bid",
                "s_ask",
                "reward $/day",
                "cost $/day",
                "collateral $",
                "RoC/day",
                "stress bid $",
                "stress ask $",
                "stress $",
                "n_bid",
                "n_ask",
                "flags",
            ],
            top_rows,
        )
        + (
            f"\n\nPending markouts (no prior at all, kept out of the ranking): {', '.join(pending)}\n"
            if pending
            else "\n"
        )
    )

    # 2. venue / category breakdown
    brk: defaultdict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for key, a in allocs.items():
        if a.s > 0:
            r = info[key]
            b = brk[(r["venue"], r["category"])]
            b[0] += 1
            b[1] += a.reward_day - a.cost_day
            b[2] += a.collateral
            b[3] += a.stress_loss
    s2 = "## 2. Venue / category breakdown\n\n" + _table(
        ["venue", "category", "half-markets", "net $/day", "collateral $", "RoC/day", "stress $"],
        [
            [v, c, int(b[0]), b[1], b[2], b[1] / b[2] if b[2] else 0.0, b[3]]
            for (v, c), b in sorted(brk.items())
        ],
    )

    # 3. domain sleeve candidates (model_needed; not excluded for lacking a reference)
    sl: dict[str, dict[str, Any]] = {}
    for h, r in halves:
        if (
            h.key in eligible_keys
            and r["category"] in DOMAIN_CATS
            and r["reference_quality"] == "model_needed"
        ):
            cap = math.floor(MKT_CAP * BUDGET / h.coll + 1e-9) if h.coll > 0 else 0
            if h.stress_per > 0:
                cap = min(cap, math.floor(STRESS_MKT * BUDGET / h.stress_per + 1e-9))
            s0 = s_star(h, cap)
            g = sl.setdefault(
                h.market,
                {
                    "cat": r["category"],
                    "net": 0.0,
                    "col": 0.0,
                    "lb": False,
                    "title": r["title"],
                    "st": 0.0,
                    "fl": set(),
                },
            )
            g["net"] += h.hours24 * ev(h, s0)
            g["col"] += s0 * h.coll
            g["st"] += s0 * h.stress_per
            g["lb"] = g["lb"] or h.c_lower_bound
            g["fl"].update(an.flags[h.key])
    s3 = (
        "## 3. Domain sleeve candidates\n\nmodel_needed markets in metals, energy, rates, FX and "
        "equity indices, ranked by standalone RoC/day at s* (no budget caps; stress cap applied). "
        "Never excluded for lacking a reference.\n\n"
        + _table(
            [
                "market",
                "category",
                "title",
                "net $/day",
                "collateral $",
                "RoC/day",
                "stress $",
                "flags",
            ],
            [
                [
                    k,
                    g["cat"],
                    g["title"],
                    g["net"],
                    g["col"],
                    g["net"] / g["col"] if g["col"] else 0.0,
                    g["st"],
                    " ".join(sorted(g["fl"] | ({"cost lower bound"} if g["lb"] else set()))),
                ]
                for k, g in sorted(
                    sl.items(),
                    key=lambda kv: kv[1]["net"] / kv[1]["col"] if kv[1]["col"] else -1,
                    reverse=True,
                )
            ],
        )
    )

    # 4. program changes vs the scan before today's first
    programs_today = store.read("programs", date, root)
    allp = store.read("programs", None, root)
    prev = None
    if not allp.is_empty() and not programs_today.is_empty():
        first = int(programs_today["ts_ns"].min())  # type: ignore[arg-type]
        before = allp.filter(pl.col("ts_ns") < first)
        if not before.is_empty():
            prev = before.filter(pl.col("ts_ns") == before["ts_ns"].max())
    cur = (
        programs_today.filter(pl.col("ts_ns") == programs_today["ts_ns"].max())
        if not programs_today.is_empty()
        else programs_today
    )
    ch = (
        program_changes(cur, prev)
        if not cur.is_empty()
        else program_changes(
            pl.DataFrame(
                {"venue": [], "market_id": [], "raw_hash": []},
                schema={"venue": pl.String, "market_id": pl.String, "raw_hash": pl.String},
            ),
            None,
        )
    )
    s4 = (
        "## 4. Program changes\n\n"
        + (
            "First scan: all programs are new (no earlier scan to diff against).\n\n"
            if prev is None
            else ""
        )
        + _table(
            ["venue", "market", "change"],
            [list(r) for r in ch.sort("venue", "market_id").iter_rows()],
        )
    )

    # 5. exclusions and scanned-but-not-allocated
    s5 = (
        "## 5. Exclusions\n\n"
        + _table(
            ["venue", "market", "side", "reason"],
            [[r["venue"], r["market_id"], r["side"], r["excluded"]] for r in an.excluded],
        )
        + f"\n\n{an.unknown_conc} markets have unknown participant concentration (kept, not excluded).\n\n"
        "### Scanned and reported, not allocated\n\n"
        + _table(["venue", "market", "reason"], [list(b) for b in an.blocked])
    )

    # 6. venue rules
    rules = []
    for name, v in sorted(conf.venue.items()):
        raw = v.raw
        rules.append(
            [
                name,
                "yes" if v.enabled else f"no: {v.skip_reason}",
                raw.get("scoring", "[?]"),
                f"bid {raw.get('coll_bid', '[?]')} / ask {raw.get('coll_ask', '[?]')}",
                f"{raw.get('maker_fee', '[?]')} / {raw.get('maker_rebate', '[?]')}",
                raw.get("coll_source", "[?]"),
                "verified" if raw.get("coll_verified") else "[?]",
                "allocated"
                if _blocked_reason(conf, name) is None
                else "not allocated: pool scope unverified",
            ]
        )
    fx = conf.forecastex
    s6 = (
        "## 6. Venue rules\n\n"
        + _table(
            [
                "venue",
                "scanned",
                "scoring",
                "collateral per contract",
                "fee / rebate",
                "source",
                "status",
                "allocation",
            ],
            rules,
        )
        + f"\n\nForecastEx renewed incentive program: **{fx.get('renewed_program', 'unknown')}** "
        f"(checked {fx.get('checked', '?')}, source: {fx.get('source', '[?]')})\n"
    )
    s7 = (
        "## 7. DEFECT half-markets\n\n"
        f"DEFECT half-markets: {len(an.defects)}\n\n"
        "A half-market is a DEFECT when its standalone return exceeds 100% of collateral per day or "
        "its reward exceeds the pool still to be paid. Excluded from allocation; the inputs that "
        "produced it are listed so the cause (scoring model, schedule, fills, collateral) can be found.\n\n"
        + _table(
            [
                "market",
                "side",
                "reasons",
                "R_side $/hr",
                "hours24",
                "pool_remaining $",
                "D_side",
                "c $/hr/contract",
                "coll $",
                "s0",
                "reward $/day",
                "cost $/day",
                "RoC/day",
                "markout (src)",
                "fills/hr",
                "flags",
            ],
            [
                [
                    d["market"],
                    d["side"],
                    "; ".join(d["reasons"]),
                    i["R_side"],
                    i["hours24"],
                    i["pool_remaining"],
                    i["D_side"],
                    i["c"],
                    i["coll"],
                    i["s0"],
                    i["reward_day"],
                    i["cost_day"],
                    i["roc"],
                    f"{i['markout'][0]:.4f} ({i['markout'][1]})",
                    i["fills"],
                    i["flags"],
                ]
                for d in an.defects
                for i in [d["inputs"]]
            ],
        )
    )
    s7 += (
        "\n\n### Allocated above 100%/day at their allocated size (not excluded)\n\n"
        f"RoC > 100%/day at the allocated size: {len(an.over_at_alloc)}. The DEFECT test runs at the "
        "standalone size s*. When the collateral/stress budget forces a smaller size the return on the "
        "first contracts is higher (concave reward), so these are budget-limited, not defective by the "
        "rule; they are listed so they can be judged.\n\n"
        + _table(
            ["market", "side", "s", "collateral $", "RoC/day", "hours24"],
            [
                [o["market"], o["side"], o["s"], o["collateral"], o["roc"], o["hours24"]]
                for o in an.over_at_alloc
            ],
        )
    )

    def _rev_row(d: dict[str, Any]) -> list[Any]:
        x = d["inputs"]
        return [d["market"], d["side"], "; ".join(d["reasons"]), x["windows"], x["pool_window"], x["pool_remaining"],
                x["R_side"], x["hours24"], x["D_side"], x["c"], x["coll"], x["s0"], x["reward_day"], x["cost_day"],
                x["roc"], x["roc_window"], f"{x['markout'][0]:.4f} ({x['markout'][1]})", x["fills"], x["flags"]]  # fmt: skip

    s7b = (
        "## 7b. needs_review_recurring\n\n"
        f"needs_review_recurring half-markets: {len(an.review)}\n\n"
        "Recurring short-window programs (the current window plus the upcoming windows of the same series, each "
        "a new market) whose return on per-window collateral exceeds 100%/day. Not DEFECT: $/day is the sum of the "
        "scheduled windows' pools over the next 24h and the collateral is the per-window requirement, reused across "
        "windows, so a high figure is expected for a stream. Held out of allocation until adverse selection on these "
        "short-lived markets can be measured; listed with the inputs.\n\n"
        + _table(
            [
                "market",
                "side",
                "reasons",
                "windows",
                "pool/window $",
                "pool_remaining $",
                "R_side $/hr",
                "hours24",
                "D_side",
                "c $/hr/contract",
                "coll $ (per window)",
                "s0",
                "reward $/day",
                "cost $/day",
                "RoC/day",
                "RoC/window",
                "markout (src)",
                "fills/hr",
                "flags",
            ],
            [_rev_row(d) for d in an.review],
        )
    )
    metals: defaultdict[str, list[str]] = defaultdict(list)
    for k_, r_ in info.items():
        if r_["category"] == "metals":
            metals[f"{r_['venue']}:{r_['market_id']}"].append(k_)
    m_rows: list[list[Any]] = []
    m_other: list[str] = []
    for m_, keys in sorted(metals.items()):
        k0 = keys[0]
        ph, n_win = an.period_h.get(k0), an.stream[k0]
        if not (n_win >= 3 or (ph is not None and ph <= 1.0)):
            m_other.append(m_)
            continue
        sz: dict[str, float] = {}
        rd = cd = col = stl = 0.0
        for k_ in keys:
            al = allocs.get(k_)
            if al is not None and al.s > 0:
                sz[info[k_]["side"]], rd, cd, col, stl = (
                    al.s,
                    rd + al.reward_day,
                    cd + al.cost_day,
                    col + al.collateral,
                    stl + al.stress_loss,
                )
            elif k_ in an.stats:
                x = an.stats[k_]
                sz[info[k_]["side"]], rd, cd, col, stl = (
                    x["s0"],
                    rd + x["rd"],
                    cd + x["cd"],
                    col + x["col"],
                    stl + x["s0"] * an.stress_per[k_],
                )
        roc = (rd - cd) / col if col > 0 else None
        win24 = an.hours24[k0] / ph if ph else None
        mk = sorted({an.markout[k_][1] for k_ in keys})
        r0 = info[k0]
        m_rows.append(
            [m_, m_.split(":", 1)[1].split("-")[0], "/".join(sorted({an.status[k_] for k_ in keys})), ph, n_win,
             an.pool_win[k0], an.pools[k0], r0.get("target_size"), r0.get("discount"), sz.get("bid"), sz.get("ask"),
             rd, cd, col, roc, roc / win24 if roc is not None and win24 else None, stl, "/".join(mk),
             " ".join(sorted({f for k_ in keys for f in an.flags[k_]}))]
        )  # fmt: skip
    s9 = (
        "## 9. Metals 15-minute streams\n\n"
        "Metals programs that are recurring short windows (the 15-minute copper/palladium/platinum/gold series). "
        "$/day = the sum of the scheduled windows' pools over the next 24h; collateral = the per-window requirement, "
        "reused across windows; RoC/window = RoC/day / windows in 24h. Sizes are the allocated size, or the "
        "standalone s* for markets held out (status column).\n\n"
        + _table(
            [
                "market",
                "series",
                "status",
                "period h",
                "windows",
                "pool/window $",
                "pool_remaining $",
                "target",
                "discount",
                "s_bid",
                "s_ask",
                "reward $/day",
                "cost $/day",
                "collateral $ (per window)",
                "RoC/day",
                "RoC/window",
                "stress $",
                "markout src",
                "flags",
            ],
            m_rows,
        )
        + f"\n\nMetals programs with a period > 1h this scan: {len(m_other)}"
        + (f" ({', '.join(m_other)})" if m_other else "")
        + ("" if metals else ". No metals programs were scanned.")
        + "\n"
    )
    sens = sensitivity(date, root, adapters, conf)
    s8 = (
        "## 8. sharesTraded counting sensitivity\n\n"
        "Polymarket US fills come from cumulative `sharesTraded`. A: every contract counted once "
        "(total/2 per side). B: both legs of a trade counted (total/4 per side). Top 10 under each:\n\n"
        f"**Top 10 changes: {'YES' if sens['changed'] else 'no'}.** "
        f"Entered under B: {', '.join(sens['entered']) or 'none'}; left: {', '.join(sens['left']) or 'none'}.\n\n"
        + _table(
            ["market", "rank A", "rank B"],
            [[m["market"], m["rank_a"], m["rank_b"]] for m in sens["moved"]],
        )
    )
    return f"{head}\n{s1}\n\n{s2}\n\n{s3}\n\n{s4}\n\n{s5}\n\n{s6}\n\n{s7}\n\n{s7b}\n\n{s8}\n\n{s9}\n\n{quality_section(date, root)}"


def quality_stats(date: str, root: Path = store.DEFAULT_ROOT) -> dict[str, Any]:
    """Per-market stale quotes (venue_ref markets, fast-tier snapshots only), D churn (timestamps
    with both side rows) and requote cadence (burst samples)."""
    out: dict[str, Any] = {"stale": {}, "churn": {}, "churn_n": {}, "cadence": {}}
    snaps = store.read("snapshots", date, root)
    if not snaps.is_empty():
        by: defaultdict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        for r in snaps.iter_rows(named=True):
            key = f"{r['venue']}:{r['market_id']}"
            e = by[key].setdefault(int(r["ts_ns"]), {"D": 0.0, "n": 0, "r": r})
            e["D"] += r.get("D_side") or 0.0
            e["n"] += 1
        for k, d in by.items():
            order = sorted(d)
            both = [t for t in order if d[t]["n"] >= 2]  # a missing side row would read as D = 0
            cv = d_churn([d[t]["D"] for t in both])
            if cv is not None:
                out["churn"][k], out["churn_n"][k] = cv, len(both)
            pts = [
                {"ts_ns": t, "bid": d[t]["r"].get("bid"), "ask": d[t]["r"].get("ask"),
                 "bid_size": d[t]["r"].get("bid_size"), "ask_size": d[t]["r"].get("ask_size"),
                 "ref_fair": d[t]["r"].get("ref_fair"), "fee_coeff": d[t]["r"].get("taker_fee_coeff")}
                for t in order
                if d[t]["r"].get("tier") == "fast" and d[t]["r"].get("reference_quality") == "venue_ref"
            ]  # fmt: skip
            st = stale_stats(pts) if pts else None
            if st:
                out["stale"][k] = st
    bursts = store.read("bursts", date, root)
    if not bursts.is_empty():
        for (v, m), g in bursts.group_by(["venue", "market_id"], maintain_order=True):
            samples = sorted(g.iter_rows(named=True), key=lambda x: x["ts_ns"])
            gaps = [(b["ts_ns"] - a["ts_ns"]) / 1e9 for a, b in pairwise(samples)]
            dt = statistics.median(gaps) if gaps else 1.0
            iv = touch_changes(samples)
            out["cadence"][f"{v}:{m}"] = {"n": len(iv), "sample_dt": dt, "hist": cadence_histogram(iv),
                                          "flags": cadence_flags(iv, dt)}  # fmt: skip
    out["n_ref"] = len(out["stale"])
    return out


def quality_section(date: str, root: Path = store.DEFAULT_ROOT, top: int = 10) -> str:
    q = quality_stats(date, root)
    s = "## 10. Quote quality (staleness, requote cadence, D churn)\n\n"
    if q["stale"]:
        ranked = sorted(q["stale"].items(), key=lambda kv: (-kv[1]["stale_pct"], kv[0]))[:top]
        s += (
            "### Stale quotes vs venue reference\n\nFast-tier snapshots of venue_ref markets only, with a VERIFIED "
            "taker fee. Stale = best bid > ref_fair + fee + 0.01 or best ask < ref_fair - fee - 0.01. "
            "First-touch takeable $/day = displayed touch size x edge, less the order fee rounded up to the cent, "
            "one fill per stale episode, over the observed span. It ignores latency and competition for the "
            "size (optimistic) and deeper levels and refills (pessimistic): neither a bound nor a forecast. "
            "Censored runs touch the first or last snapshot (their duration is a lower bound).\n\n"
            + _table(
                [
                    "market",
                    "scored snapshots",
                    "stale_pct",
                    "episodes",
                    "censored runs",
                    "median stale run (s)",
                    "first-touch takeable $/day (fee-adjusted)",
                ],
                [
                    [
                        k,
                        v["n_scored"],
                        v["stale_pct"],
                        v["episodes"],
                        v["censored_runs"],
                        v["median_run_s"],
                        v["takeable_day"],
                    ]
                    for k, v in ranked
                ],
            )
            + "\n\n"
        )
    else:
        s += (
            "### Stale quotes vs venue reference\n\nStale-quote metrics: no market has a venue reference "
            "(reference_quality = venue_ref with a ref_fair from the venue, and a verified taker fee) and "
            "fast-tier snapshots yet. No adapter supplies a reference fair value today, so nothing is scored; "
            "wire a `reference_fair` source per venue to enable this.\n\n"
        )
    ch = sorted(q["churn"].items(), key=lambda kv: -kv[1])[:15]
    s += (
        "### D churn (coefficient of variation of D across snapshots)\n\nNeeds at least 5 snapshots with both side rows.\n\n"
        + _table(["market", "D churn (CV)", "snapshots"], [[k, v, q["churn_n"][k]] for k, v in ch])
        + "\n\n### Touch requote cadence (burst samples)\n\n"
    )
    if q["cadence"]:
        rows = []
        for k, c in sorted(q["cadence"].items()):
            fl = ", ".join(
                f"clustering at {p}s ({f['share']:.0%} of intervals vs {f['expected']:.0%} expected if random)"
                for p, f in c["flags"].items()
                if f["flag"]
            )
            rows.append([k, c["n"], c["sample_dt"], *c["hist"].values(), fl or "-"])
        s += (
            "Touch PRICE changes only; periods shorter than 3 sampling intervals are not testable.\n\n"
            + _table(
                [
                    "market",
                    "touch changes",
                    "sample dt (s)",
                    *next(iter(q["cadence"].values()))["hist"].keys(),
                    "flags",
                ],
                rows,
            )
        )
    else:
        s += "No burst samples for this date: run `bmm scan burst` (a 5-minute snapshot cannot resolve 5/30/60 s requoting).\n"
    return s


def write_report(
    date: str | None, root: Path = store.DEFAULT_ROOT, out_dir: Path = Path("reports")
) -> int:
    d = date or datetime.fromtimestamp(now_ns() / 1e9, tz=UTC).strftime("%Y-%m-%d")
    md = build_report(d, root)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"rewards_scan_{d}.md"
    path.write_text(md)
    print(f"wrote {path}")
    return 0
