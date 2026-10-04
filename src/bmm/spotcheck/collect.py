"""Snapshot collector: ladder, reference future and matched option chain per product.

Market data only. Each product is isolated: a failure never stops the others.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import structlog

from bmm.clock import ct_to_ns
from bmm.spotcheck.contracts import ASSUMPTIONS, ProductSpec, expiry_ts_ns, match_expiry
from bmm.spotcheck.normalise import (
    EMPTY,
    NormRow,
    RawQuote,
    ScaleError,
    check_ladder_scale,
    cme_row,
    forecastex_row,
)
from bmm.spotcheck.schema import ParquetSink
from bmm.spotcheck.source import MarketDataSource, SourceError
from bmm.spotcheck.vol import atm_sigma, strike_window, year_frac

MIN_NS = 60 * 1_000_000_000
GRID_COARSE_NS = 5 * MIN_NS
MAX_CONSECUTIVE_FAILURES = 3
ATM_NEIGHBOURS = 5
CAUGHT = (SourceError, ScaleError, asyncio.TimeoutError, ConnectionError)


def next_snapshot_ns(now_ns: int, fix_ts_ns: int) -> int | None:
    """Next slot at or after `now_ns`: 5-min grid until fix-30m, 1-min grid until fix+30m."""
    if now_ns <= fix_ts_ns - 30 * MIN_NS:
        return fix_ts_ns - ((fix_ts_ns - now_ns) // GRID_COARSE_NS) * GRID_COARSE_NS
    if now_ns <= fix_ts_ns + 30 * MIN_NS:
        return fix_ts_ns - ((fix_ts_ns - now_ns) // MIN_NS) * MIN_NS
    return None


def _ladder_rows(
    spec: ProductSpec,
    quotes: list[Any],
    snapshot_id: str,
    ts_ns: int,
    question_date: date,
    fix_ts: int,
) -> list[dict[str, Any]]:
    by_k: dict[float, dict[str, RawQuote]] = {}
    for q in quotes:
        by_k.setdefault(q.strike, {})[q.right] = q.quote
    norms: list[tuple[float, NormRow]] = []
    for k in sorted(by_k):
        sides = by_k[k]
        if spec.venue == "forecastex":
            n = forecastex_row(k, sides.get("C", EMPTY), sides.get("P", EMPTY))
        else:
            n = cme_row(k, sides.get("C"), sides.get("P"), spec.payout_scale)
        norms.append((k, n))
    if spec.venue == "cme":
        check_ladder_scale([n for _, n in norms], spec.payout_scale)
    scale = spec.payout_scale if spec.venue == "cme" else 1.0
    rows: list[dict[str, Any]] = []
    for k, n in norms:
        y, no = n.raw_yes, n.raw_no
        rows.append({
            "snapshot_id": snapshot_id, "ts_ns": ts_ns, "product": spec.key,
            "question_date": question_date, "fix_ts_ns": fix_ts, "strike": k,
            "yes_bid": n.row.yes_bid, "yes_ask": n.row.yes_ask,
            "bid_size": n.row.bid_size, "ask_size": n.row.ask_size, "crossed": n.crossed,
            "raw_yes_bid": y.bid, "raw_yes_bid_size": y.bid_size,
            "raw_yes_ask": y.ask, "raw_yes_ask_size": y.ask_size,
            "raw_no_bid": no.bid if no else None, "raw_no_bid_size": no.bid_size if no else None,
            "raw_no_ask": no.ask if no else None, "raw_no_ask_size": no.ask_size if no else None,
            "last": None if y.last is None else y.last / scale,  # per-$1, like yes_bid
            "last_size": y.last_size, "volume": y.volume, "payout_scale": spec.payout_scale,
        })  # fmt: skip
    return rows


def _check_fix_time(
    spec: ProductSpec, details: list[dict[str, Any]], qd: date, fix_ts: int, log: Any
) -> None:
    if spec.venue != "forecastex":
        return
    for lt, tz in sorted({(str(d.get("lastTradeTime", "")), str(d.get("timeZoneId", ""))) for d in details}):
        try:
            ts = expiry_ts_ns(qd.strftime("%Y%m%d"), lt, tz or "America/Chicago")
        except ValueError:
            log.warning("lastTradeTime unparseable", product=spec.key, last_trade_time=lt, tz=tz)
            continue
        if ts != fix_ts:
            log.warning(
                "ForecastEx lastTradeTime differs from configured fix_ct",
                product=spec.key, last_trade_time=lt, tz=tz, fix_ct=spec.fix_ct,
            )  # fmt: skip


async def collect_once(
    source: MarketDataSource,
    spec: ProductSpec,
    question_date: date,
    sink: ParquetSink,
    snapshot_id: str,
    ts_ns: int,
    gc_month: str | None,
    write_details: bool = False,
    log: Any = None,
) -> None:
    """One snapshot for one product. Raises SourceError/ScaleError/timeouts; writes nothing then."""
    log = log or structlog.get_logger()
    fix_ts = ct_to_ns(question_date, *spec.fix_ct)
    quotes, details = await source.ladder(spec, question_date)
    ladder_rows = _ladder_rows(spec, quotes, snapshot_id, ts_ns, question_date, fix_ts)
    fut = await source.reference_future(spec, question_date, details, gc_month)
    fq = fut.quote
    if fq.bid is not None and fq.ask is not None:
        F = 0.5 * (fq.bid + fq.ask)
    elif fq.last is not None:
        F = fq.last
    else:
        raise SourceError(f"no price for reference future {fut.local_symbol}")
    fut_row = {
        "snapshot_id": snapshot_id, "ts_ns": ts_ns, "product": spec.key, "con_id": fut.con_id,
        "local_symbol": fut.local_symbol, "contract_month": fut.contract_month,
        "bid": fq.bid, "ask": fq.ask, "bid_size": fq.bid_size, "ask_size": fq.ask_size,
        "last": fq.last,
    }  # fmt: skip

    option_rows: list[dict[str, Any]] = []
    match = match_expiry(fix_ts, fut.con_id, await source.option_expiries(spec, fut))
    if match is None:
        log.warning("no options for reference future", product=spec.key, con_id=fut.con_id)
    else:
        ladder_strikes = [r["strike"] for r in ladder_rows]
        for expiry, role in match.chosen:
            if expiry.expiry_ts_ns < ts_ns - 1_000_000_000:
                log.warning("option expiry already passed", product=spec.key, role=role)
                continue
            T = year_frac(ts_ns, expiry.expiry_ts_ns)
            strikes = sorted(await source.listed_strikes(spec, expiry))
            if not strikes:
                continue
            i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - F))
            atm = strikes[max(0, i - ATM_NEIGHBOURS) : i + ATM_NEIGHBOURS + 1]
            chain = await source.calls(spec, expiry, atm)
            sigma = atm_sigma(chain, F, T)
            if sigma is not None:  # pass 2: the full +-4 sigma window, widened to the ladder
                lo, hi = strike_window(F, sigma, T, ladder_strikes=ladder_strikes, listed=strikes)
                extra = [k for k in strikes if lo <= k <= hi and k not in set(atm)]
                chain = chain + await source.calls(spec, expiry, extra)
                sigma = atm_sigma(chain, F, T) or sigma
            for c in sorted(chain, key=lambda c: c.strike):
                option_rows.append({
                    "snapshot_id": snapshot_id, "ts_ns": ts_ns, "product": spec.key,
                    "underlying_con_id": expiry.underlying_con_id,
                    "expiry_ts_ns": expiry.expiry_ts_ns, "expiry_role": role,
                    "expiry_mismatch": match.mismatch, "strike": c.strike,
                    "call_bid": c.call_bid, "call_ask": c.call_ask, "und_px": F,
                    "atm_sigma": sigma,
                })  # fmt: skip

    sink.append(question_date, "ladder", ladder_rows)
    sink.append(question_date, "futures", [fut_row])
    if option_rows:
        sink.append(question_date, "options", option_rows)
    if write_details:
        note = [{"assumption": ASSUMPTIONS[spec.key]}] if spec.key in ASSUMPTIONS else []
        sink.write_contracts(question_date, spec.key, details + note)
        _check_fix_time(spec, details, question_date, fix_ts, log)
        if note:
            log.warning("assumed fix time", product=spec.key, note=ASSUMPTIONS[spec.key])


async def collect(
    source: MarketDataSource,
    products: list[ProductSpec],
    question_date: date,
    sink: ParquetSink,
    clock: Callable[[], int],
    sleep: Callable[[float], Awaitable[None]],
    gc_month: str | None = None,
    log: Any = None,
) -> dict[str, str]:
    """Run until every product is past fix+30m or dropped. Returns product -> "ok" | error."""
    log = log or structlog.get_logger()
    fixes = {p.key: ct_to_ns(question_date, *p.fix_ct) for p in products}
    specs = {p.key: p for p in products}
    after = {k: clock() for k in specs}
    fails = dict.fromkeys(specs, 0)
    oks = dict.fromkeys(specs, 0)
    last_err = dict.fromkeys(specs, "")
    active = set(specs)
    while active:
        slots: dict[str, int] = {}
        for k in sorted(active):
            s = next_snapshot_ns(after[k], fixes[k])
            if s is None:
                active.discard(k)
            else:
                slots[k] = s
        if not slots:
            break
        t = min(slots.values())
        wait = (t - clock()) / 1e9
        if wait > 0:
            await sleep(wait)
        for k, s in sorted(slots.items()):
            if s != t:
                continue
            ts = clock()
            try:
                await collect_once(
                    source, specs[k], question_date, sink, f"{k}-{ts}", ts, gc_month,
                    write_details=oks[k] == 0, log=log,
                )  # fmt: skip
            except CAUGHT as e:
                fails[k] += 1
                last_err[k] = f"{type(e).__name__}: {e}"
                log.warning("product snapshot failed", product=k, error=last_err[k], n=fails[k])
                if fails[k] >= MAX_CONSECUTIVE_FAILURES:
                    active.discard(k)
                    log.error("product dropped for session", product=k)
            else:
                fails[k] = 0
                oks[k] += 1
            after[k] = max(s + 1, clock())
    out: dict[str, str] = {}
    for k in specs:
        if fails[k] >= MAX_CONSECUTIVE_FAILURES:
            out[k] = f"dropped after {fails[k]} consecutive failures: {last_err[k]}"
        elif fails[k]:
            out[k] = last_err[k]
        elif oks[k]:
            out[k] = "ok"
        else:
            out[k] = "no snapshots taken (outside the fix window)"
    return out
