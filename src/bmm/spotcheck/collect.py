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
from bmm.spotcheck.contracts import (
    ASSUMPTIONS,
    ProductSpec,
    expiry_ts_ns,
    match_expiry,
    option_class_ok,
)
from bmm.spotcheck.normalise import (
    EMPTY,
    NormRow,
    RawQuote,
    ScaleError,
    check_ladder_scale,
    cme_row,
    forecastex_row,
)
from bmm.spotcheck.safety import SafetyError
from bmm.spotcheck.schema import ParquetSink
from bmm.spotcheck.source import FutQuote, MarketDataSource, SourceError
from bmm.spotcheck.vol import atm_sigma, digital_delta, strike_window, year_frac

MIN_NS = 60 * 1_000_000_000
GRID_COARSE_NS = 5 * MIN_NS
MAX_CONSECUTIVE_FAILURES = 3
ATM_NEIGHBOURS = 5
DF_FLAG_THRESHOLD = 0.01  # |dF| x digital delta, per $1
EDGE_EXTRA_STRIKES = 2
CAUGHT = (SourceError, ScaleError, asyncio.TimeoutError, ConnectionError)


def next_snapshot_ns(now_ns: int, fix_ts_ns: int) -> int | None:
    """Next slot at or after `now_ns`: 5-min grid until fix-30m, 1-min grid until fix+30m."""
    if now_ns <= fix_ts_ns - 30 * MIN_NS:
        return fix_ts_ns - ((fix_ts_ns - now_ns) // GRID_COARSE_NS) * GRID_COARSE_NS
    if now_ns <= fix_ts_ns + 30 * MIN_NS:
        return fix_ts_ns - ((fix_ts_ns - now_ns) // MIN_NS) * MIN_NS
    return None


def _sz(price: float | None, size: int) -> int | None:
    """A size is only meaningful next to a price; a missing side logs None, not 0."""
    return size if price is not None else None


def _mid(fut: FutQuote, spec: ProductSpec, log: Any) -> float:
    fq = fut.quote
    if fq.bid is not None and fq.ask is not None:
        return 0.5 * (fq.bid + fq.ask)
    if fq.last is not None:
        log.warning("future priced from last (no live two-sided quote)", product=spec.key)
        return fq.last
    raise SourceError(f"no price for reference future {fut.local_symbol}")


def _ladder_rows(
    spec: ProductSpec,
    quotes: list[Any],
    snapshot_id: str,
    ts_ns: int,
    question_date: date,
    fix_ts: int,
    ladder_ts: int,
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
            "post_fix": ts_ns >= fix_ts, "ladder_ts_ns": ladder_ts,
            "raw_yes_bid": y.bid, "raw_yes_bid_size": _sz(y.bid, y.bid_size),
            "raw_yes_ask": y.ask, "raw_yes_ask_size": _sz(y.ask, y.ask_size),
            "raw_no_bid": no.bid if no else None,
            "raw_no_bid_size": _sz(no.bid, no.bid_size) if no else None,
            "raw_no_ask": no.ask if no else None,
            "raw_no_ask_size": _sz(no.ask, no.ask_size) if no else None,
            "last": None if y.last is None else y.last / scale,  # per-$1, like yes_bid
            "last_size": y.last_size, "volume": y.volume, "payout_scale": spec.payout_scale,
        })  # fmt: skip
    return rows


def _check_fix_time(
    spec: ProductSpec, details: list[dict[str, Any]], qd: date, fix_ts: int, log: Any
) -> None:
    if spec.venue != "forecastex":
        return
    for lt, tz in sorted(
        {(str(d.get("lastTradeTime", "")), str(d.get("timeZoneId", ""))) for d in details}
    ):
        try:
            ts = expiry_ts_ns(qd.strftime("%Y%m%d"), lt, tz or "America/Chicago")
        except ValueError:
            log.warning("lastTradeTime unparseable", product=spec.key, last_trade_time=lt, tz=tz)
            continue
        if ts != fix_ts:
            log.warning(
                "ForecastEx lastTradeTime (trading close) differs from configured fix_ct (settlement fix); "
                "this can be legitimate, check the rulebook",
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
    clock: Callable[[], int] | None = None,
) -> None:
    """One snapshot for one product. Raises SourceError/ScaleError/timeouts; writes nothing then.

    `ts_ns` is the slot time. The ladder, the future and the options are fetched one after the
    other, so each table records when its own fetch completed (`clock`), the future is re-quoted
    after the options, and F is the mean of the two. `df_flag` marks a snapshot where the move
    between the two quotes times the digital delta at the ATM ladder strike exceeds 1c.
    """
    log = log or structlog.get_logger()
    now = clock or (lambda: ts_ns)
    fix_ts = ct_to_ns(question_date, *spec.fix_ct)
    quotes, details = await source.ladder(spec, question_date)
    ladder_ts = now()
    ladder_rows = _ladder_rows(spec, quotes, snapshot_id, ts_ns, question_date, fix_ts, ladder_ts)
    fut = await source.reference_future(spec, question_date, details, gc_month)
    fut_ts = now()
    f_before = _mid(fut, spec, log)
    fq = fut.quote

    # vanilla chains only: never the venue's own event-contract class nor any EC* class
    expiries = [
        e
        for e in await source.option_expiries(spec, fut, question_date)
        if option_class_ok(spec, e.trading_class)
    ]
    match = match_expiry(fix_ts, fut.con_id, expiries)
    chains: list[tuple[Any, str, list[Any], int]] = []  # expiry, role, quotes, completed at
    if match is None:
        log.warning("no options for reference future", product=spec.key, con_id=fut.con_id)
    else:
        ladder_strikes = [r["strike"] for r in ladder_rows]
        for expiry, role in match.chosen:
            if expiry.expiry_ts_ns <= ts_ns:
                log.warning("option expiry already passed", product=spec.key, role=role)
                continue
            T = year_frac(ts_ns, expiry.expiry_ts_ns)
            strikes = sorted(await source.listed_strikes(spec, expiry))
            if not strikes:
                continue
            i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - f_before))
            atm = strikes[max(0, i - ATM_NEIGHBOURS) : i + ATM_NEIGHBOURS + 1]
            chain = await source.calls(spec, expiry, atm)
            sigma = atm_sigma(chain, f_before, T)
            if sigma is None:
                log.warning("atm_sigma unresolved; fetching the ladder range only",
                            product=spec.key, role=role)  # fmt: skip
            # pass 2: the +-4 sigma window (ladder range only if sigma is unresolved)
            lo, hi = strike_window(
                f_before, sigma, T, ladder_strikes=ladder_strikes, listed=strikes
            )
            have = {c.strike for c in chain}
            extra = [k for k in strikes if lo <= k <= hi and k not in have]
            chain = chain + await source.calls(spec, expiry, extra)
            # qualification can drop edge strikes: if the ladder is no longer bracketed, reach out
            for side in ("lo", "hi"):
                got = sorted(c.strike for c in chain)
                if not got:
                    break
                if side == "lo" and got[0] > min(ladder_strikes):
                    more = [k for k in strikes if k < got[0]][-EDGE_EXTRA_STRIKES:]
                elif side == "hi" and got[-1] < max(ladder_strikes):
                    more = [k for k in strikes if k > got[-1]][:EDGE_EXTRA_STRIKES]
                else:
                    continue
                if more:
                    chain = chain + await source.calls(spec, expiry, more)
            if not chain:
                log.warning("option chain empty", product=spec.key, role=role)
                continue
            chains.append((expiry, role, chain, now()))

    f_after = f_before
    if chains:
        try:
            f_after = _mid(
                await source.reference_future(spec, question_date, details, gc_month), spec, log
            )
        except CAUGHT as e:
            log.warning("future re-quote failed; using the first quote", product=spec.key,
                        error=str(e))  # fmt: skip
    F = 0.5 * (f_before + f_after)

    option_rows: list[dict[str, Any]] = []
    deltas: list[float] = []
    atm_k = min((r["strike"] for r in ladder_rows), key=lambda k: abs(k - F))
    for expiry, role_s, chain, opt_ts in chains:
        T = year_frac(ts_ns, expiry.expiry_ts_ns)
        sigma = atm_sigma(chain, F, T)
        if sigma is not None and match is not None:
            deltas.append(digital_delta(F, atm_k, sigma, T))
        for c in sorted(chain, key=lambda c: c.strike):
            option_rows.append({
                "snapshot_id": snapshot_id, "ts_ns": ts_ns, "product": spec.key,
                "underlying_con_id": expiry.underlying_con_id,
                "expiry_ts_ns": expiry.expiry_ts_ns, "trading_class": expiry.trading_class,
                "expiry_role": role_s, "expiry_mismatch": match.mismatch if match else False,
                "strike": c.strike, "call_bid": c.call_bid, "call_ask": c.call_ask,
                "call_bid_size": getattr(c, "call_bid_size", None),
                "call_ask_size": getattr(c, "call_ask_size", None),
                "opt_ts_ns": opt_ts, "und_px": F, "atm_sigma": sigma,
            })  # fmt: skip
    df_flag = bool(deltas) and abs(f_after - f_before) * max(deltas) > DF_FLAG_THRESHOLD + 1e-12
    if df_flag:
        log.warning("future moved during the snapshot", product=spec.key, f_before=f_before,
                    f_after=f_after)  # fmt: skip
    fut_row = {
        "snapshot_id": snapshot_id, "ts_ns": ts_ns, "product": spec.key, "con_id": fut.con_id,
        "local_symbol": fut.local_symbol, "contract_month": fut.contract_month,
        "bid": fq.bid, "ask": fq.ask, "bid_size": fq.bid_size, "ask_size": fq.ask_size,
        "last": fq.last, "fut_ts_ns": fut_ts, "und_px_before": f_before,
        "und_px_after": f_after, "df_flag": df_flag,
    }  # fmt: skip

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
                    write_details=oks[k] == 0, log=log, clock=clock,
                )  # fmt: skip
            except SafetyError:
                raise  # never swallowed: it ends the session
            except Exception as e:  # one product's bug must not stop the others
                fails[k] += 1
                expected = isinstance(e, CAUGHT)
                last_err[k] = f"{type(e).__name__}: {e}"
                log.warning(
                    "product snapshot failed", product=k, error=last_err[k], n=fails[k],
                    exc_info=not expected,
                )  # fmt: skip
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
