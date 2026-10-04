"""Markdown + PNG report: eight sections per product, a pinned-rule verdict and a summary.

Statistics come from `metrics`; the verdict from `rule` (owner-owned constants). Verdict inputs
are pre-fix and exclude crossed rows; the verdict fee is `rule.VERDICT_FEE`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import polars as pl

from bmm.spotcheck import metrics, rule
from bmm.spotcheck.contracts import PRODUCTS

FEES = (0.01, 0.02)
INK, MUTED, GRID = "#1f2933", "#7b8794", "#d9dee4"
BAR, BAR2, THRESH = "#2f6f9f", "#a9c6dd", "#b5483a"  # one blue family plus a single accent
FEE_SOURCES = (
    "ForecastEx: 0.01 per side ([V] rules03242641956). CME event contract exchange fee: 0.01 per "
    "contract ([V] research/venue_facts.md, Fees)."
)
OWNER_CAVEAT = (
    "The IBKR commission is unknown (owner item #3), so every statistic is shown at fee per side "
    "0.01 and 0.02; the verdict uses 0.02. Option quotes are treated as undiscounted (intraday)."
)

Fmt = Callable[[Any], str]


def _c(x: Any) -> str:  # cents
    return "" if x is None else f"{x * 100:.2f}"


def _pct(x: Any) -> str:
    return "" if x is None else f"{x * 100:.0f}%"


def _n(x: Any) -> str:
    if x is None:
        return ""
    return f"{x:g}" if isinstance(x, float) else str(x)


def _f4(x: Any) -> str:
    return "" if x is None else f"{x:.4g}"


def _md(
    df: pl.DataFrame, fmt: Mapping[str, Fmt] | None = None, rename: Mapping[str, str] | None = None
) -> str:
    if df.height == 0:
        return "_no rows_\n"
    fmt = fmt or {}
    rename = rename or {}
    head = "| " + " | ".join(rename.get(c, c) for c in df.columns) + " |"
    sep = "|" + "|".join("---" for _ in df.columns) + "|"
    lines = [head, sep]
    for r in df.iter_rows(named=True):
        lines.append(
            "| "
            + " | ".join(fmt.get(c, _n)(r[c]) if r[c] is not None else "" for c in df.columns)
            + " |"
        )
    return "\n".join(lines) + "\n"


def _order_buckets(df: pl.DataFrame, col: str, order: tuple[str, ...]) -> pl.DataFrame:
    if df.height == 0:
        return df
    rank = {b: i for i, b in enumerate(order)}
    return df.with_columns(_r=pl.col(col).replace_strict(rank, default=99)).sort("_r").drop("_r")


def _style(ax: Any, xlabel: str, ylabel: str, title: str) -> None:
    ax.set_xlabel(xlabel, color=INK)
    ax.set_ylabel(ylabel, color=INK)
    ax.set_title(title, color=INK, fontsize=10, loc="left")
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(MUTED)
    ax.tick_params(colors=INK)


def _gap_chart(gs: pl.DataFrame, product: str, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(6, 3.4), dpi=110, facecolor="white")
    ax.set_facecolor("white")
    gs = _order_buckets(gs, "m_bucket", metrics.MONEYNESS_BUCKETS)
    if gs.height:
        xs = list(range(gs.height))
        med = [v * 100 for v in gs["median_abs_gap"]]
        ax.bar(xs, med, color=BAR, width=0.6, label="median |gap|")
        lo = [(m - i / 2) * 100 for m, i in zip(gs["median_abs_gap"], gs["iqr"], strict=True)]
        hi = [(m + i / 2) * 100 for m, i in zip(gs["median_abs_gap"], gs["iqr"], strict=True)]
        ax.vlines(xs, lo, hi, color=INK, lw=1.2, label="median +- IQR/2")
        for x, v, n in zip(xs, med, gs["n"], strict=True):
            ax.annotate(f"n={n}", (x, v), ha="center", va="bottom", fontsize=7, color=MUTED,
                        xytext=(0, 2), textcoords="offset points")  # fmt: skip
        ax.set_xticks(xs, list(gs["m_bucket"]))
        ax.axhline(rule.GO_MEDIAN_ABS_GAP * 100, color=THRESH, ls="--", lw=1)
        ax.axhline(rule.KILL_MEDIAN_ABS_GAP * 100, color=MUTED, ls=":", lw=1)
        ax.text(
            len(xs) - 0.5,
            rule.GO_MEDIAN_ABS_GAP * 100,
            " GO 3c",
            color=THRESH,
            fontsize=7,
            va="bottom",
            ha="right",
        )
        ax.text(
            len(xs) - 0.5,
            rule.KILL_MEDIAN_ABS_GAP * 100,
            " KILL 2c",
            color=MUTED,
            fontsize=7,
            va="top",
            ha="right",
        )
        ax.legend(frameon=False, fontsize=7, loc="upper left")
    else:
        ax.text(0.5, 0.5, "no data", ha="center", va="center", color=MUTED, transform=ax.transAxes)
    _style(ax, "moneyness |ln K/F| / (sigma sqrt T) bucket", "venue mid minus fair, abs (cents)",
           f"{product}: mid gap vs fair by moneyness")  # fmt: skip
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)


def _persist_chart(ps: pl.DataFrame, product: str, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(6, 3.4), dpi=110, facecolor="white")
    ax.set_facecolor("white")
    ps = _order_buckets(ps, "m_bucket", metrics.MONEYNESS_BUCKETS)
    if ps.height:
        xs = list(range(ps.height))
        w = 0.38
        ax.bar([x - w / 2 for x in xs], ps["median_run"], width=w, color=BAR, label="median run")
        ax.bar([x + w / 2 for x in xs], ps["max_run"], width=w, color=BAR2, label="max run")
        ax.set_xticks(xs, list(ps["m_bucket"]))
        ax.axhline(rule.GO_MIN_PERSISTENCE, color=THRESH, ls="--", lw=1)
        ax.legend(frameon=False, fontsize=7, loc="upper left")
    else:
        ax.text(0.5, 0.5, "no runs with |gap| > 3c", ha="center", va="center", color=MUTED,
                transform=ax.transAxes)  # fmt: skip
    _style(ax, "moneyness bucket (bucket at run start)", "run length (consecutive snapshots)",
           f"{product}: persistence of |gap| > 3c")  # fmt: skip
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)


def _gap_pair(with_floor: pl.DataFrame, without: pl.DataFrame) -> pl.DataFrame:
    """The '>2' bucket row with and without tick-floor rows, stacked and labelled."""
    a = with_floor.filter(pl.col("m_bucket") == ">2").with_columns(m_bucket=pl.lit(">2 (all rows)"))
    b = without.filter(pl.col("m_bucket") == ">2").with_columns(
        m_bucket=pl.lit(">2 (fair in 0.01..0.99)")
    )
    return pl.concat([a, b])


def _with_scope(df: pl.DataFrame, scope: str) -> pl.DataFrame:
    return df.with_columns(scope=pl.lit(scope))


def _section_ladder(lq: pl.DataFrame, product: str) -> str:
    d = lq.filter(pl.col("product") == product).drop("product")
    return _md(d, {"median_width": _c, "median_depth": _n, "volume_delta": _n},
               {"median_width": "median width (c)", "median_depth": "median depth (bid+ask)"}) + (
        "\nCrossed/locked rows (YES bid + NO bid >= 1) are counted here and excluded from every "
        "gap, taker and persistence statistic.\n")  # fmt: skip


def write_report(
    data_root: Path,
    d_from: date,
    d_to: date,
    out_dir: Path,
    name: str | None = None,
    nonsim_threshold: float = 0.01,
) -> Path:
    tables = metrics.load(Path(data_root), d_from, d_to)
    if tables["ladder"].height == 0:
        raise ValueError(f"no ladder data under {data_root} for {d_from}..{d_to}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    label = name or f"{d_from.isoformat()}_{d_to.isoformat()}"
    md_path = out_dir / f"spotcheck_{label}.md"

    ef = {f: metrics.edge_frame(tables, f, nonsim_threshold) for f in FEES}
    efv = ef[rule.VERDICT_FEE]
    efm = efv.filter(~pl.col("expiry_mismatch"))
    present = set(tables["ladder"]["product"].unique().to_list())
    products = [k for k in PRODUCTS if k in present] + sorted(present - set(PRODUCTS))

    sim_n = metrics.sim_counts(efv)

    def n_snaps(p: str, b: str) -> tuple[int, int]:
        d = sim_n.filter((pl.col("product") == p) & (pl.col("sim_bucket") == b))
        return (int(d["snapshots"][0]), int(d["rows"][0])) if d.height else (0, 0)

    gap_all, gap_m = metrics.gap_stats(efv), metrics.gap_stats(efm)
    NS = "non-simultaneous"
    gap_ns = metrics.gap_stats(efv, sim=NS)
    gap_ns_out = metrics.gap_stats(efv, sim=NS, value="gap_outside")
    ns_extra = metrics.nonsim_extra(efv)
    gap_all_incl = metrics.gap_stats(efv, sim="adjusted")
    gap_m_incl = metrics.gap_stats(efm, sim="adjusted")
    gap_raw_incl = metrics.gap_stats(efv, sim="all")
    gap_nofloor = metrics.gap_stats(efv, floor_rows=False)
    share = metrics.sim_share(efv)
    gap_ttf = metrics.gap_stats(efv, by=("ttf_bucket",))
    taker = metrics.taker_stats(efv, FEES)
    taker_ns = metrics.taker_stats(efv, FEES, sim=NS)
    grid = tables["ladder"].select("product", "fix_ts_ns", "ts_ns").unique()  # every snapshot
    # verdict series: non-simultaneous snapshots count via gap_outside (see metrics.persistence)
    pers_all = metrics.persistence(efv, grid=grid, sim="adjusted")
    pers_m = metrics.persistence(efm, grid=grid, sim="adjusted")  # mismatched snapshots break runs
    pers_ns = metrics.persistence(efv, grid=grid, sim=NS, value="gap_outside")
    persist = pl.concat([_with_scope(pers_all, "all"), _with_scope(pers_m, "matched")])
    sz_all = metrics.size_at_gap_strikes(efv, sim="adjusted")
    sz_m = metrics.size_at_gap_strikes(efm, sim="adjusted")
    sizes = pl.concat([_with_scope(sz_all, "all"), _with_scope(sz_m, "matched")])
    lq = metrics.ladder_quality(tables)
    pfa = metrics.post_fix_activity(tables, efv)
    mism = metrics.mismatch_share(efv)
    arb = metrics.arb_table(efv, tables)
    arb_tot = metrics.arb_totals(arb)
    tilt = metrics.mismatch_tilt(efv)

    out: list[str] = [
        f"# Spot check report: {label}",
        "",
        f"- Question dates: {d_from.isoformat()} to {d_to.isoformat()}",
        f"- Products: {', '.join(products)}",
        f"- Fee runs (per side): {', '.join(f'{f:.2f}' for f in FEES)}; verdict at "
        f"{rule.VERDICT_FEE:.2f}",
        f"- Fee sources: {FEE_SOURCES}",
        f"- Caveat: {OWNER_CAVEAT}",
        "- Gap = venue mid - model-free-anchored fair (`analyze.fair_band`); positive = venue rich.",
        "- Post-fix rows: a matched expiry is no longer listed (no option rows), and a mismatched "
        "later chain may still have rows but they are deliberately ignored. Fair for the "
        "post-fix window always uses the last pre-fix chain for that product. Post-fix rows are "
        "excluded from all headline statistics.",
        f"- Non-simultaneity threshold: |dF| x digital delta > {nonsim_threshold * 100:g}c "
        "(`--nonsim-threshold`; recomputed from the stored before/after future quotes).",
        "",
    ]
    verdicts: list[tuple[str, rule.Verdict]] = []
    for p in products:
        pf = lambda df, p=p: df.filter(pl.col("product") == p).drop("product")
        v = rule.verdict(p, gap_all, gap_m, persist, sizes, taker,
                         gap_all_incl=gap_all_incl, gap_matched_incl=gap_m_incl,
                         gap_all_raw_incl=gap_raw_incl)  # fmt: skip
        verdicts.append((p, v))
        gpng = out_dir / f"spotcheck_{label}_{p}_gap_by_moneyness.png"
        ppng = out_dir / f"spotcheck_{label}_{p}_persistence.png"
        _gap_chart(pf(gap_all), p, gpng)
        _persist_chart(pf(pers_all), p, ppng)
        gfmt = {
            "median_abs_gap": _c,
            "iqr": _c,
            "pct_gt_2c": _pct,
            "pct_gt_3c": _pct,
            "pct_snapshots_gt_2c": _pct,
            "pct_snapshots_gt_3c": _pct,
        }
        gren = {"median_abs_gap": "median abs gap (c)", "iqr": "IQR (c)", "pct_gt_2c": "> 2c (% obs)",
                "pct_gt_3c": "> 3c (% obs)",
                "pct_snapshots_gt_2c": "> 2c (% snaps)", "pct_snapshots_gt_3c": "> 3c (% snaps)"}  # fmt: skip
        out += [f"## {p}", "", "### 1. Ladder quality", "", _section_ladder(lq, p)]
        out += [
            "### 2. Mid gap vs fair", "",
            "Simultaneity: a snapshot is **simultaneous** when the reference future did not move "
            "enough between its before and after quotes (which bracket the ladder and options once "
            "the future is cached) to shift the digital by more than 1c (`df_flag` False); "
            "**non-simultaneous** when it did, or when the move is unknown (`df_flag` True or "
            "null). It is recomputed here from the stored before/after future quotes (the "
            f"collector's `df_flag` is information only) with threshold {nonsim_threshold * 100:g}c, "
            "ATM ladder strike, the chain's sigma and T to the fix. **A missing futures row or an "
            "unknown before/after quote counts as non-simultaneous.** Non-simultaneous "
            "snapshots are kept and reported below. Per row, "
            "w = |digital delta at the row's own strike| x |dF| widens the fair band, and "
            "gap outside = sign(gap) x max(|gap| - w, 0). The verdict's gap and taker statistics "
            "use the simultaneous bucket only; its persistence and size inputs also count a "
            "non-simultaneous snapshot as a hit when |gap outside| > 3c. "
            f"Pre-fix snapshots: {n_snaps(p, 'simultaneous')[0]} simultaneous "
            f"({n_snaps(p, 'simultaneous')[1]} rows), {n_snaps(p, NS)[0]} non-simultaneous "
            f"({n_snaps(p, NS)[1]} rows).", "",
            "**Simultaneous bucket** by moneyness (pre-fix, non-crossed observations, "
            "expiry-mismatched "
            "chains included). `% obs` = share of strike-snapshot observations with |gap| above "
            "the threshold (what the owner rule uses); `% snaps` = share of snapshots whose "
            "median |gap| in the bucket exceeds it:", "",
            _md(_order_buckets(pf(gap_all), "m_bucket", metrics.MONEYNESS_BUCKETS), gfmt, gren),
            "By time to fix (post-fix shown for information only):", "",
            _md(_order_buckets(pf(gap_ttf), "ttf_bucket", metrics.TTF_BUCKETS), gfmt, gren),
            f"![gap by moneyness]({gpng.name})", "",
            "Simultaneous share of pre-fix snapshots by time to fix:", "",
            _md(_order_buckets(pf(share), "ttf_bucket", metrics.TTF_BUCKETS), {"share": _pct}),
            "Tick-floor check: the `>2` bucket with and without rows whose fair is < 0.01 or "
            "> 0.99 (simultaneous bucket). The rule input is unchanged (rows included); the "
            "owner decides whether the rule should exclude tick-floor rows:", "",
            _md(_gap_pair(pf(gap_all), pf(gap_nofloor)), gfmt, gren),
            "**Non-simultaneous bucket**, gap as observed:", "",
            _md(_order_buckets(pf(gap_ns), "m_bucket", metrics.MONEYNESS_BUCKETS), gfmt, gren),
            "Non-simultaneous bucket, gap outside the widened band (what the futures move cannot "
            "explain):", "",
            _md(_order_buckets(pf(gap_ns_out), "m_bucket", metrics.MONEYNESS_BUCKETS), gfmt, gren),
            "Median band widening and fetch-interval move:", "",
            _md(_order_buckets(pf(ns_extra), "m_bucket", metrics.MONEYNESS_BUCKETS),
                {"median_w": _c, "median_dF": _f4},
                {"median_w": "median w (c)", "median_dF": "median dF (pts)",
                 "n_w_null": "rows with w unknown"}),
        ]  # fmt: skip
        tk = pf(taker)
        out += [
            "### 3. Taker edge after fees", "",
            "Best edge per snapshot = max over strikes of buy (fair - ask - fee) and sell "
            "(bid - fair - fee). Capturable = sum of positive edge x displayed taker-side size x "
            "payout scale, $ per snapshot, at edge threshold 0 (and above 1c and 2c, medians), "
            "at fee 0.01 and 0.02. Simultaneous bucket (used by the verdict):", "",
            _md(tk, {"fee": lambda x: f"{x:.2f}", "pct_snapshots_best_edge_gt_1c": _pct,
                     "pct_snapshots_best_edge_gt_2c": _pct, "pct_snapshots_best_edge_le_0": _pct,
                     "capturable_median": lambda x: f"{x:.2f}",
                     "capturable_p90": lambda x: f"{x:.2f}",
                     "capturable_gt_1c_median": lambda x: f"{x:.2f}",
                     "capturable_gt_2c_median": lambda x: f"{x:.2f}"},
                {"pct_snapshots_best_edge_gt_1c": "best > 1c", "pct_snapshots_best_edge_gt_2c":
                 "best > 2c", "pct_snapshots_best_edge_le_0": "best <= 0",
                 "capturable_median": "capturable median ($)", "capturable_p90": "capturable p90 ($)",
                 "capturable_gt_1c_median": "cap. median, edge > 1c ($)",
                 "capturable_gt_2c_median": "cap. median, edge > 2c ($)",
                 "fee": "fee/side"}),
            "Non-simultaneous bucket (edges against the point fair, not the widened band):", "",
            _md(pf(taker_ns), {"fee": lambda x: f"{x:.2f}", "pct_snapshots_best_edge_gt_1c": _pct,
                               "pct_snapshots_best_edge_gt_2c": _pct,
                               "pct_snapshots_best_edge_le_0": _pct,
                               "capturable_median": lambda x: f"{x:.2f}",
                               "capturable_p90": lambda x: f"{x:.2f}",
                               "capturable_gt_1c_median": lambda x: f"{x:.2f}",
                               "capturable_gt_2c_median": lambda x: f"{x:.2f}"}),
        ]  # fmt: skip
        a = pf(arb)
        out += ["### 4. Model-free arb edges", ""]
        if a.height == 0:
            out += [f"No venue quote beat the replication-grade option bounds after fee "
                    f"{rule.VERDICT_FEE:g}.", ""]  # fmt: skip
        else:
            out += [
                f"{a.height} rows beat the bounds after fee {rule.VERDICT_FEE:g} "
                f"({int(a['suspect'].sum())} suspect: option width > 50% of mid, option fetch more "
                "than 60 s from the ladder fetch, post-fix, carried chain, or band widening w "
                "unknown). Edges are model-free bounds from option bid/ask, widened by a delta "
                "approximation for the futures move during the fetch (not replication-grade when "
                "w > 0). "
                "$ = positive edge x displayed taker-side size x payout scale.", "",
                _md(pf(arb_tot), {"arb_usd": lambda x: f"{x:.2f}"},
                    {"arb_usd": "arb $ (edge x size)"}),
                _md(a.drop("snapshot_id").head(25),
                    {"ts_ns": _n, "arb_usd": lambda x: f"{x:.2f}", "arb_buy_edge": _c, "arb_sell_edge": _c, "yes_bid": _f4,
                     "yes_ask": _f4, "opt_ts_lag_ms": lambda x: f"{x:.0f}",
                     "opt_rel_width": lambda x: f"{x:.3f}"}),
            ]  # fmt: skip
        ps = pf(persist)
        out += [
            "### 5. Persistence", "",
            "Runs of consecutive snapshots with |gap| > 3c per strike; scope `all` includes "
            "mismatched chains, `matched` only expiry-matched ones. Simultaneous bucket (used by "
            "the verdict; a non-simultaneous snapshot breaks a run):", "",
            _md(_order_buckets(ps, "m_bucket", metrics.MONEYNESS_BUCKETS)),
            "Non-simultaneous bucket, runs of gap outside the widened band > 3c:", "",
            _md(_order_buckets(pf(pers_ns), "m_bucket", metrics.MONEYNESS_BUCKETS)),
            f"![persistence]({ppng.name})", "",
        ]  # fmt: skip
        out += ["### 6. Post-settlement window", "",
                "Post-fix quotes resting and trades (visible only as volume steps); trade price is "
                "compared with the last pre-fix fair at that strike.", "",
                _md(pf(pfa), {"median_trade_px_vs_fair": _c, "median_trade_px_vs_outcome": _c,
                              "volume_after_fix": _n},
                    {"median_trade_px_vs_fair": "median trade px - last fair (c)",
                     "median_trade_px_vs_outcome": "median trade px - settled outcome (c)"}),
                "Trades are volume steps on both the YES and NO tickers (for ForecastEx it is [?] "
                "whether a matched execution prints on both, so counts may double count). "
                "Outcome = 1 if the first post-fix future mid is above the strike.", ""]  # fmt: skip
        out += ["### 7. Expiry mismatch", "",
                "Share of snapshots priced off an option expiry different from the venue fix, and "
                "the gap on matched vs mismatched chains (bias_abs > 0: mismatched chains show "
                "larger gaps).", "",
                _md(pf(mism), {"share_mismatched": _pct, "median_gap_matched": _c,
                               "median_gap_mismatched": _c, "median_abs_gap_matched": _c,
                               "median_abs_gap_mismatched": _c, "bias_abs": _c}),
                "Tilt by chain role = median of (mid - fair) x sign(F - K) in cents. If the "
                "mismatch drives the gap, a later-expiry (`above`) chain should show tilt > 0 and "
                "an earlier (`below`) chain tilt < 0:", "",
                _md(pf(tilt), {"median_tilt": _c})]  # fmt: skip
        nums = pl.DataFrame({"quantity": list(v.numbers), "value": list(v.numbers.values())})
        out += [
            "### 8. Verdict", "",
            f"**{v.label}**", "",
            *[f"- {r}" for r in v.reasons], "",
            f"Rule constants: GO gap >= {rule.GO_MEDIAN_ABS_GAP * 100:g}c, > 3c in >= "
            f"{rule.GO_PCT_GT_3C:.0%}, run >= {rule.GO_MIN_PERSISTENCE}, size >= "
            f"{rule.GO_MIN_MEDIAN_SIZE} (matched only); KILL gap < {rule.KILL_MEDIAN_ABS_GAP * 100:g}c "
            f"everywhere or taker <= 0 in > {rule.KILL_PCT_TAKER_LE_0:.0%} at fee "
            f"{rule.VERDICT_FEE:g}.", "",
            "Numbers (gaps in price units, 0.01 = 1c):", "",
            _md(nums, {"value": _f4}),
        ]  # fmt: skip

    out += ["## Summary", ""]
    for p, v in verdicts:
        keys = [k for k in v.numbers if k.endswith("median_abs_gap") and not k.startswith("info.")]
        top = max((v.numbers[k] for k in keys), default=None)
        le0 = v.numbers.get("pct_snapshots_best_edge_le_0")
        drive = f"max bucket median |gap| {top * 100:.2f}c" if top is not None else "no data"
        if le0 is not None:
            drive += f", taker best edge <= 0 in {le0:.0%} of snapshots at fee {rule.VERDICT_FEE:g}"
        info = next((r for r in v.reasons if r.startswith("info:")), "")
        tail = ""
        if info:
            tail = (
                "; with non-simultaneous rows included: "
                + info.split("would give ")[1].split(";")[0]
            )
        out.append(f"- {p}: **{v.label}** ({drive}{tail})")
    out.append("")
    md_path.write_text("\n".join(out))
    return md_path
