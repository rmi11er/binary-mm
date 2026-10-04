"""Fill rates, maker markouts, exclusions and collateral expressions (spec 60). Pure functions."""

from __future__ import annotations

import ast
import bisect
from collections.abc import Callable

import polars as pl

from bmm.scan.models import Book, MarketMeta, Side, Trade

H_NS = 3600 * 10**9
MARKOUT_NS = 30 * 60 * 10**9
MARKOUT_TOL_NS = 5 * 60 * 10**9
DOMAIN_FREE_CRYPTO_MIN_NS = 24 * H_NS


def side_of(trade: Trade, mid: float | None) -> Side | None:
    """Which resting side the trade filled: an aggressive sell hits our bid, a buy lifts our ask."""
    if trade.aggressor == "sell":
        return "bid"
    if trade.aggressor == "buy":
        return "ask"
    if mid is None or trade.price == mid:
        return None
    return "bid" if trade.price < mid else "ask"


def _mid_series(mids: pl.DataFrame) -> tuple[list[int], list[float]]:
    if mids.is_empty() or "mid" not in mids.columns:
        return [], []
    m = mids.drop_nulls(["ts_ns", "mid"]).sort("ts_ns")
    return m["ts_ns"].to_list(), m["mid"].to_list()


def _asof(ts: int, tss: list[int], vals: list[float], tol_ns: int | None) -> float | None:
    """Latest value at or before ts (within tol_ns if given)."""
    i = bisect.bisect_right(tss, ts) - 1
    if i < 0 or (tol_ns is not None and ts - tss[i] > tol_ns):
        return None
    return vals[i]


def fills_contracts_hr(
    trades: list[Trade], side: Side, mids: pl.DataFrame, window_ns: int
) -> float:
    """Contracts (not trades) that hit `side` per hour over the window."""
    tss, vals = _mid_series(mids)
    total = sum(t.size for t in trades if side_of(t, _asof(t.ts_ns, tss, vals, None)) == side)
    return total / (window_ns / H_NS)


def markout_30m(trades: pl.DataFrame, mids: pl.DataFrame, side: Side) -> tuple[float | None, int]:
    """Size-weighted maker markout vs the mid 30 min after each fill, $/contract.

    Negative = adverse to the maker. Returns (None, 0) when no fill has a mid at +30 min.
    """
    tss, vals = _mid_series(mids)
    if trades.is_empty() or not tss:
        return None, 0
    num = den = 0.0
    n = 0
    for ts, price, size, aggr in trades.select("ts_ns", "price", "size", "aggressor").iter_rows():
        t = Trade(ts, price, size, aggr)
        if side_of(t, _asof(ts, tss, vals, None)) != side:
            continue
        later = _asof(ts + MARKOUT_NS, tss, vals, MARKOUT_TOL_NS)
        if later is None:
            continue
        num += size * ((later - price) if side == "bid" else (price - later))
        den += size
        n += 1
    return (num / den, n) if den > 0 else (None, 0)


def exclusion(meta: MarketMeta, book: Book, now_ns: int) -> str | None:
    """Reason to exclude the market from the ranking, or None. Unknown concentration is kept."""
    if meta.in_game:
        return "live in-game"
    if (
        meta.category == "crypto"
        and meta.expiry_ns is not None
        and meta.expiry_ns - now_ns < DOMAIN_FREE_CRYPTO_MIN_NS
    ):
        return "crypto <1d to expiry"
    for lv in book.bids[:1] + book.asks[:1]:
        if lv.top_holder_share is not None and lv.top_holder_share > 0.5:
            return "one participant >50% at touch"
    return None


_FUNCS: dict[str, Callable[..., float]] = {"max": max, "min": min}


def eval_coll(expr: str | None, p: float) -> float:
    """Evaluate a collateral rule like "p", "1-p" or "max(p, 1-p)" at price p.

    Missing or unverified ("[?]...") rules fall back to p; callers pass the side default
    explicitly. Only arithmetic over p is allowed.
    """
    if expr is None or expr.strip().startswith("["):
        expr = "p"

    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, int | float):
            return float(n.value)
        if isinstance(n, ast.Name) and n.id == "p":
            return p
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub | ast.UAdd):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add | ast.Sub | ast.Mult | ast.Div):
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            return a * b if isinstance(n.op, ast.Mult) else a / b
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in _FUNCS
            and not n.keywords
        ):
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError(f"unsupported collateral expression: {expr!r}")

    return ev(ast.parse(expr, mode="eval"))


# --- proxied (backfilled / derived) quantities. Everything below feeds fields flagged `proxied`. ---
STEP_NS = 5 * 60 * 10**9


def resample_trades(trades: list[Trade], step_ns: int = STEP_NS) -> list[tuple[int, float]]:
    """Last trade price per 5-minute bucket, forward-filled: a mid proxy for venues with only a tape."""
    if not trades:
        return []
    ts = sorted(trades, key=lambda t: t.ts_ns)
    out: list[tuple[int, float]] = []
    i, px = 0, ts[0].price
    for b in range(ts[0].ts_ns // step_ns, ts[-1].ts_ns // step_ns + 1):
        while i < len(ts) and ts[i].ts_ns // step_ns <= b:
            px = ts[i].price
            i += 1
        out.append((b * step_ns, px))
    return out


def _moves(series: list[tuple[int, float]], lag_ns: int, tol_ns: int) -> list[float]:
    if len(series) < 2:
        return []
    s = sorted(series)
    tss, vals = [t for t, _ in s], [v for _, v in s]
    out = []
    for t, v in s:
        if t + lag_ns > tss[-1]:
            break
        later = _asof(t + lag_ns, tss, vals, tol_ns)
        if later is not None:
            out.append(abs(later - v))
    return out


def mean_abs_move(
    series: list[tuple[int, float]], lag_ns: int, tol_ns: int = 10 * 60 * 10**9
) -> tuple[float | None, int]:
    """Mean |mid(t + lag) - mid(t)| over the series; (None, 0) when no pair exists."""
    m = _moves(series, lag_ns, tol_ns)
    return (sum(m) / len(m), len(m)) if m else (None, 0)


def tail_move_1h(fine: list[tuple[int, float]], coarse: list[tuple[int, float]]) -> float | None:
    """Empirical 99th percentile of |1h mid move|. `fine` is a ~5-minute series (1h lag);
    `coarse` is a 3-hour series whose 3h moves are scaled by 1/sqrt(3). Takes the larger."""
    import numpy as np

    h = 3600 * 10**9
    cands: list[float] = []
    f = _moves(fine, h, 10 * 60 * 10**9)
    if len(f) >= 20:
        cands.append(float(np.percentile(f, 99)))
    c = _moves(coarse, 3 * h, 90 * 60 * 10**9)
    if len(c) >= 10:
        cands.append(float(np.percentile(c, 99)) / 3**0.5)
    return max(cands) if cands else None


def calibrate_lambda(rows: list[tuple[float, float, float]]) -> float | None:
    """lambda = sum(n|markout|) / sum(n * mean|30m move|) over (n, markout, move) rows from venues
    with a tape, clamped to [0.05, 1]. Converts a tape-less venue's 30m mid movement to a markout."""
    num = sum(n * abs(m) for n, m, _ in rows)
    den = sum(n * v for n, _, v in rows)
    return None if den <= 0 else min(1.0, max(0.05, num / den))


def adverse_jump(side: Side, price: float, jump_class: str, p99: float | None) -> float:
    """Adverse price jump per contract: a bid loses if the price gaps toward 0, an ask toward 1.
    jump_class high -> the full distance; otherwise the empirical 1h tail capped at that distance."""
    dist = min(1.0, max(0.0, price if side == "bid" else 1.0 - price))
    if jump_class == "high" or p99 is None:
        return dist
    return min(p99, dist)


def fills_from_shares(
    cur_shares: float, cur_ts: int, opened_ns: int | None, prev: tuple[int, float] | None
) -> float | None:
    """Total contracts/hr traded from cumulative sharesTraded: delta vs an earlier snapshot when it
    is at least an hour old, else the lifetime average since the market opened."""
    life = None
    if opened_ns is not None and cur_ts > opened_ns:
        life = cur_shares / max(1.0, (cur_ts - opened_ns) / H_NS)
    if prev is not None:
        pts, psh = prev
        gap = (cur_ts - pts) / H_NS
        if gap >= 1.0 and cur_shares >= psh:
            return (cur_shares - psh) / gap
    return life
