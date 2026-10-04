"""Report builder: reads the parquet tables, rebuilds each half-market and allocates (spec 60).

Does no network I/O. Venue scoring comes from the adapters' pure methods, so the report is
reproducible from `data/scan/` alone.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from bmm.clock import now_ns
from bmm.scan import config as cfgmod
from bmm.scan import store
from bmm.scan.allocator import Alloc, Half, allocate, cost_hr, ev, s_star, shrink_markout
from bmm.scan.config import ScanConfig
from bmm.scan.models import Book, Level, Program, VenueAdapter

DOMAIN_CATS = {"metals", "energy", "rates", "fx", "equity_index"}
BUDGET = 8000.0
MKT_CAP = 0.15
TOP_N = 25


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


def build_report(
    date: str,
    root: Path = store.DEFAULT_ROOT,
    adapters: Mapping[str, Any] | None = None,
    conf: ScanConfig | None = None,
) -> str:
    snaps_all = store.read("snapshots", date, root)
    head = f"# Rewards scan {date}\n"
    if snaps_all.is_empty():
        return head + f"\nNo snapshots for {date}.\n"
    conf = conf or _load_conf()
    adapters = adapters if adapters is not None else _default_adapters(conf)
    snaps = _latest(snaps_all, ["venue", "market_id", "side"])
    books = {
        (r["venue"], r["market_id"]): _book(r)
        for r in _latest(store.read("books", date, root), ["venue", "market_id"]).iter_rows(
            named=True
        )
    }
    programs_today = store.read("programs", date, root)
    progs: dict[tuple[str, str], Program] = {}
    fields = Program.__dataclass_fields__.keys()
    for r in _latest(programs_today, ["venue", "market_id"]).iter_rows(named=True):
        progs[(r["venue"], r["market_id"])] = Program(**{k: r[k] for k in fields})

    cat_mean, overall = _cat_means(snaps)
    halves: list[tuple[Half, dict[str, Any]]] = []
    excluded: list[dict[str, Any]] = []
    unknown_conc = 0
    for r in snaps.iter_rows(named=True):
        k = (r["venue"], r["market_id"])
        if r["excluded"]:
            excluded.append(r)
            continue
        if r["side"] == "bid" and r["top_holder_share"] is None:
            unknown_conc += 1
        ad, prog, book = adapters.get(r["venue"]), progs.get(k), books.get(k)
        if ad is None or prog is None or book is None:
            continue
        m, lb = shrink_markout(
            r["markout_30m_side"],
            r["n_markout_side"] or 0,
            cat_mean.get(r["category"], overall),
        )
        c = cost_hr(
            r["fills_side_hr"],
            r["depth_raw_side"],
            m,
            r["maker_fee"],
            r["maker_rebate"],
            r["coll_side"],
        )
        side = r["side"]
        h = Half(
            key=f"{r['venue']}:{r['market_id']}:{side}",
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
        )
        halves.append((h, r))

    measured = [h for h, _ in halves if not h.c_lower_bound]
    allocs: dict[str, Alloc] = {a.key: a for a in allocate(measured, BUDGET, MKT_CAP)}
    info = {h.key: r for h, r in halves}

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
                "rd": 0.0,
                "cd": 0.0,
                "col": 0.0,
                "n_bid": 0,
                "n_ask": 0,
            },
        )
        g["s_" + r["side"]] += a.s
        g["n_" + r["side"]] = r["n_markout_side"] or 0
        g["rd"] += a.reward_day
        g["cd"] += a.cost_day
        g["col"] += a.collateral
    ranked = sorted(
        agg.items(), key=lambda kv: (kv[1]["rd"] - kv[1]["cd"]) / kv[1]["col"], reverse=True
    )
    top_rows = [
        [i, k, g["r"]["category"], g["s_bid"], g["s_ask"], g["rd"], g["cd"], g["col"],
         (g["rd"] - g["cd"]) / g["col"], g["n_bid"], g["n_ask"]]
        for i, (k, g) in enumerate(ranked[:TOP_N], 1)
    ]  # fmt: skip
    pending = sorted({h.market for h, _ in halves if h.c_lower_bound})
    s1 = (
        f"## 1. Top 25 markets\n\nBudget ${BUDGET:,.0f}; market cap {MKT_CAP:.0%}; venue cap 40%. "
        "Net of fill cost. n_bid / n_ask = fills with a 30m markout (markout confidence).\n\n"
        + _table(
            [
                "#",
                "market",
                "category",
                "s_bid",
                "s_ask",
                "reward $/day",
                "cost $/day",
                "collateral $",
                "RoC/day",
                "n_bid",
                "n_ask",
            ],
            top_rows,
        )
        + (
            f"\n\nPending markouts (no category prior, kept out of the ranking): {', '.join(pending)}\n"
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
    s2 = "## 2. Venue / category breakdown\n\n" + _table(
        ["venue", "category", "half-markets", "net $/day", "collateral $", "RoC/day"],
        [
            [v, c, int(b[0]), b[1], b[2], b[1] / b[2] if b[2] else 0.0]
            for (v, c), b in sorted(brk.items())
        ],
    )

    # 3. domain sleeve candidates (model_needed; not excluded for lacking a reference)
    sl: dict[str, dict[str, Any]] = {}
    for h, r in halves:
        if r["category"] in DOMAIN_CATS and r["reference_quality"] == "model_needed":
            cap = math.floor(MKT_CAP * BUDGET / h.coll + 1e-9) if h.coll > 0 else 0
            s0 = s_star(h, cap)
            g = sl.setdefault(
                h.market,
                {"cat": r["category"], "net": 0.0, "col": 0.0, "lb": False, "title": r["title"]},
            )
            g["net"] += 24 * ev(h, s0)
            g["col"] += s0 * h.coll
            g["lb"] = g["lb"] or h.c_lower_bound
    s3 = (
        "## 3. Domain sleeve candidates\n\nmodel_needed markets in metals, energy, rates, FX and "
        "equity indices, ranked by standalone RoC/day at s* (no budget caps). Never excluded for "
        "lacking a reference.\n\n"
        + _table(
            [
                "market",
                "category",
                "title",
                "net $/day",
                "collateral $",
                "RoC/day",
                "markout prior",
            ],
            [
                [
                    k,
                    g["cat"],
                    g["title"],
                    g["net"],
                    g["col"],
                    g["net"] / g["col"] if g["col"] else 0.0,
                    "none (cost lower bound)" if g["lb"] else "ok",
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
    allp = store.read("programs", None, root)
    prev = None
    if not allp.is_empty():
        first = int(programs_today["ts_ns"].min())  # type: ignore[arg-type]
        before = allp.filter(pl.col("ts_ns") < first)
        if not before.is_empty():
            prev = before.filter(pl.col("ts_ns") == before["ts_ns"].max())
    cur = programs_today.filter(pl.col("ts_ns") == programs_today["ts_ns"].max())
    ch = program_changes(cur, prev)
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

    # 5. exclusions
    s5 = (
        "## 5. Exclusions\n\n"
        + _table(
            ["venue", "market", "side", "reason"],
            [[r["venue"], r["market_id"], r["side"], r["excluded"]] for r in excluded],
        )
        + f"\n\n{unknown_conc} markets have unknown participant concentration (kept, not excluded).\n"
    )

    # 6. venue rules
    rules = []
    for name, v in sorted(conf.venue.items()):
        raw = v.raw
        ok = "verified" if raw.get("coll_verified") else "[?]"
        rules.append(
            [
                name,
                "yes" if v.enabled else f"no: {v.skip_reason}",
                raw.get("scoring", "[?]"),
                f"bid {raw.get('coll_bid', '[?]')} / ask {raw.get('coll_ask', '[?]')}",
                f"{raw.get('maker_fee', '[?]')} / {raw.get('maker_rebate', '[?]')}",
                raw.get("coll_source", "[?]"),
                ok,
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
            ],
            rules,
        )
        + f"\n\nForecastEx renewed incentive program: **{fx.get('renewed_program', 'unknown')}** "
        f"(checked {fx.get('checked', '?')}, source: {fx.get('source', '[?]')})\n"
    )
    return f"{head}\n{s1}\n\n{s2}\n\n{s3}\n\n{s4}\n\n{s5}\n\n{s6}"


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
