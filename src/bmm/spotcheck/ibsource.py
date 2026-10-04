"""Read-only IB market-data source (paper gateway on 4002 only).

Permitted ib_async calls: connectAsync (readonly=True), reqContractDetailsAsync, reqTickersAsync,
reqSecDefOptParamsAsync, qualifyContractsAsync, disconnect (plus managedAccounts to verify a paper
login). Order-placement methods are replaced with stubs that raise SafetyError, on the IB object
and on its client, before the socket is opened.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import date, timedelta
from typing import Any

import structlog
from ib_async import IB, Contract, Future, FuturesOption, StartupFetch

from bmm.clock import now_ns, ns_to_ct
from bmm.spotcheck.analyze import OptQuote
from bmm.spotcheck.contracts import (
    OptExpiry,
    ProductSpec,
    expiry_ts_ns,
    lead_month,
    option_class_ok,
    parse_gc_month,
)
from bmm.spotcheck.normalise import RawQuote, live_quote, raw_quote
from bmm.spotcheck.safety import (
    SafetyError,
    check_accounts,
    check_client_id,
    check_host,
    check_mode,
    check_port,
)
from bmm.spotcheck.source import FutQuote, LadderQuote, SizedOptQuote, SourceError

# Lower-case on purpose: matched case-insensitively against dir(ib) / dir(ib.client).
_ARMED = re.compile(
    "placeorder|cancelorder|globalcancel|whatiforder|bracketorder|onecancelsall|exerciseoptions",
    re.IGNORECASE,
)
_DETAIL_WINDOW_DAYS = 7


def _stub(name: str) -> Callable[..., Any]:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise SafetyError(f"spotcheck is market-data only; {name} is disabled")

    return refuse


def disarm(ib: Any) -> list[str]:
    """Replace every order-surface method on `ib` and `ib.client` with a raising stub."""
    done: list[str] = []
    for label, obj in (("ib", ib), ("client", ib.client)):
        for name in dir(obj):
            if _ARMED.search(name):
                object.__setattr__(obj, name, _stub(f"{label}.{name}"))  # shadows the class method
                done.append(f"{label}.{name}")
    return done


def _details_row(cd: Any) -> dict[str, Any]:
    c = cd.contract
    return {
        "conId": c.conId,
        "localSymbol": c.localSymbol,
        "right": c.right,
        "strike": c.strike,
        "multiplier": c.multiplier,
        "minTick": cd.minTick,
        "priceMagnifier": cd.priceMagnifier,
        "longName": cd.longName,
        "lastTradeTime": cd.lastTradeTime,
        "timeZoneId": cd.timeZoneId,
        "underConId": cd.underConId,
    }


def _quote(t: Any) -> RawQuote:
    return raw_quote(t.bid, t.ask, t.bidSize, t.askSize, t.last, t.lastSize, t.volume)


class IBSource:
    def __init__(self, ib: Any, max_lines: int = 90, timeout_s: float = 30.0) -> None:
        self._ib = ib
        self._max = max(1, max_lines)
        self._timeout = timeout_s
        self._strikes: dict[tuple[str, str], list[float]] = {}
        self._log = structlog.get_logger()

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        client_id: int,
        max_lines: int = 90,
        ib_factory: Callable[[], Any] = IB,
        allow_hosts: Iterable[str] = (),
    ) -> IBSource:
        check_mode(os.environ)
        check_port(port)  # before anything is constructed or opened
        check_client_id(client_id)
        check_host(host, allow_hosts)
        ib = ib_factory()
        disarm(ib)
        try:
            # StartupFetch(0): skip the default positions/executions/account-update pulls, which run
            # before the paper-account check below.
            await ib.connectAsync(
                host, port, clientId=client_id, readonly=True, fetchFields=StartupFetch(0)
            )
            check_accounts(ib.managedAccounts())
        except BaseException:
            ib.disconnect()
            raise
        return cls(ib, max_lines)

    async def close(self) -> None:
        self._ib.disconnect()

    # -- helpers -----------------------------------------------------------------------------
    async def _w(self, aw: Awaitable[Any]) -> Any:
        return await asyncio.wait_for(aw, self._timeout)

    async def _details(self, contract: Any) -> list[Any]:
        return list(await self._w(self._ib.reqContractDetailsAsync(contract)) or [])

    async def _tickers(self, contracts: list[Any]) -> list[Any]:
        out: list[Any] = []
        for i in range(0, len(contracts), self._max):
            out += list(await self._w(self._ib.reqTickersAsync(*contracts[i : i + self._max])))
        return out

    # -- MarketDataSource --------------------------------------------------------------------
    async def ladder(
        self, spec: ProductSpec, question_date: date
    ) -> tuple[list[LadderQuote], list[dict[str, Any]]]:
        d = question_date.strftime("%Y%m%d")
        if spec.venue == "forecastex":
            probe = Contract(
                secType="OPT", exchange="FORECASTX", symbol=spec.ib_symbol,
                lastTradeDateOrContractMonth=d, currency="USD",
            )  # fmt: skip
        else:
            probe = Contract(
                secType="FOP", exchange=spec.ib_exchange, symbol=spec.ib_symbol,
                tradingClass=spec.trading_class or "",
                lastTradeDateOrContractMonth=d, currency="USD",
            )  # fmt: skip
        details = [cd for cd in await self._details(probe) if cd.contract.right in ("C", "P")]
        if not details:
            raise SourceError("no ladder listed")
        tickers = await self._tickers([cd.contract for cd in details])
        quotes = [
            LadderQuote(cd.contract.strike, cd.contract.right, _quote(t))
            for cd, t in zip(details, tickers, strict=False)
        ]
        if all(q.quote.bid is None and q.quote.ask is None for q in quotes):
            venue = "FORECASTX" if spec.venue == "forecastex" else spec.ib_symbol
            raise SourceError(f"no {venue} data on paper")
        return quotes, [_details_row(cd) for cd in details]

    def _cross_check_month(
        self, spec: ProductSpec, details: list[dict[str, Any]], qd: date, contract_month: str
    ) -> None:
        """The month named in the question (e.g. "Dec-26") must equal the lead-month future's."""
        months = {
            m for d in details
            if (m := parse_gc_month(str(d.get("longName", "")), qd, anchor=None, strict_year=True))
        }  # fmt: skip
        if not months:
            self._log.warning("no contract month named in question text", product=spec.key)
        elif months != {contract_month}:
            raise SourceError(
                f"{spec.key}: question names {sorted(months)} but lead-month future is "
                f"{contract_month}"
            )

    async def reference_future(
        self,
        spec: ProductSpec,
        question_date: date,
        ladder_details: list[dict[str, Any]],
        gc_month: str | None,
    ) -> FutQuote:
        if spec.ref_rule == "lead_quarterly":
            futs = await self._details(
                Future(symbol=spec.fut_symbol, exchange=spec.fut_exchange, currency="USD")
            )
            by_date = {
                cd.realExpirationDate: cd for cd in futs if getattr(cd, "realExpirationDate", "")
            }
            if not by_date:
                raise SourceError(f"no {spec.fut_symbol} futures listed")
            dates = [date(int(s[:4]), int(s[4:6]), int(s[6:8])) for s in by_date]
            target = lead_month(question_date, dates).strftime("%Y%m%d")
            cd = by_date[target]
            self._cross_check_month(spec, ladder_details, question_date, cd.contractMonth)
        elif spec.ref_rule == "named_gc":
            months = {
                m for d in ladder_details
                if (m := parse_gc_month(str(d.get("longName", "")), question_date))
            }  # fmt: skip
            if len(months) > 1:
                raise SourceError(f"ladder names several gold contract months: {sorted(months)}")
            parsed = next(iter(months), None)
            if parsed and gc_month and parsed != gc_month:
                raise SourceError(
                    f"--gc-month {gc_month} disagrees with the month named in the ladder {parsed}"
                )
            month = parsed or gc_month
            if not month:
                raise SourceError("METLG month unresolved; pass --gc-month")
            futs = await self._details(
                Future(
                    symbol=spec.fut_symbol, exchange=spec.fut_exchange,
                    lastTradeDateOrContractMonth=month, currency="USD",
                )
            )  # fmt: skip
            if not futs:
                raise SourceError(f"no {spec.fut_symbol} future for {month}")
            matching = [f for f in futs if f.contractMonth == month]
            if len(matching) != 1:
                raise SourceError(
                    f"{len(matching)} {spec.fut_symbol} futures with contractMonth {month}"
                )
            cd = matching[0]
        else:  # under_con_id
            ids = {int(d["underConId"]) for d in ladder_details if d.get("underConId")}
            if len(ids) != 1:
                raise SourceError(f"underConId unresolved from ladder details: {sorted(ids)}")
            futs = await self._details(Contract(conId=ids.pop()))
            if not futs:
                raise SourceError("underlying future not found")
            cd = futs[0]
        tickers = await self._tickers([cd.contract])
        q = live_quote(_quote(tickers[0])) if tickers else raw_quote(None, None, 0, 0)
        c = cd.contract
        month_s = cd.contractMonth or c.lastTradeDateOrContractMonth[:6]
        return FutQuote(c.conId, c.localSymbol, month_s, q)

    async def option_expiries(
        self, spec: ProductSpec, fut: FutQuote, question_date: date | None = None
    ) -> list[OptExpiry]:
        chains = await self._w(
            self._ib.reqSecDefOptParamsAsync(spec.fut_symbol, spec.opt_exchange, "FUT", fut.con_id)
        )
        day0 = question_date or ns_to_ct(now_ns()).date()
        horizon = {
            (day0 + timedelta(days=i)).strftime("%Y%m%d") for i in range(_DETAIL_WINDOW_DAYS + 1)
        }
        out: list[OptExpiry] = []
        for ch in chains or []:
            if not option_class_ok(spec, str(ch.tradingClass)):
                self._log.info("option class excluded", cls=ch.tradingClass, product=spec.key)
                continue
            strikes = sorted(float(k) for k in ch.strikes)
            if not strikes:
                continue
            for exp in sorted(set(ch.expirations) & horizon):
                probe = FuturesOption(
                    spec.fut_symbol, exp, strikes[len(strikes) // 2], "C", spec.opt_exchange,
                    tradingClass=ch.tradingClass,
                )  # fmt: skip
                cds = await self._details(probe)
                if not cds or not cds[0].lastTradeTime or not cds[0].underConId:
                    self._log.warning(
                        "option expiry dropped (no details)", cls=ch.tradingClass, expiry=exp
                    )
                    continue  # never guess an expiry time or underlying
                cd = cds[0]
                try:
                    ts = expiry_ts_ns(exp, cd.lastTradeTime, cd.timeZoneId or "America/Chicago")
                except ValueError as e:
                    raise SourceError(f"expiry time for {exp}: {e}") from e
                out.append(OptExpiry(ts, int(cd.underConId), ch.tradingClass, exp))
                self._strikes[(ch.tradingClass, exp)] = strikes
        return out

    async def listed_strikes(self, spec: ProductSpec, expiry: OptExpiry) -> list[float]:
        try:
            return list(self._strikes[(expiry.trading_class, expiry.yyyymmdd)])
        except KeyError:
            raise SourceError("strikes not loaded; call option_expiries first") from None

    async def calls(
        self, spec: ProductSpec, expiry: OptExpiry, strikes: list[float]
    ) -> list[OptQuote]:
        cons = [
            FuturesOption(
                spec.fut_symbol, expiry.yyyymmdd, k, "C", spec.opt_exchange,
                tradingClass=expiry.trading_class,
            )
            for k in strikes
        ]  # fmt: skip
        qualified: list[Any] = []
        for i in range(0, len(cons), self._max):
            got = await self._w(self._ib.qualifyContractsAsync(*cons[i : i + self._max]))
            qualified += [c for c in got if c is not None and c.conId]
        if len(qualified) < len(cons):
            self._log.warning(
                "option strikes not qualified", expiry=expiry.yyyymmdd,
                n_missing=len(cons) - len(qualified),
            )  # fmt: skip
        out: list[OptQuote] = []
        for con, t in zip(qualified, await self._tickers(qualified), strict=False):
            q = live_quote(_quote(t))  # no stale prices from size-0 sides, no crossed quotes
            if q.ask is None:
                continue
            out.append(
                SizedOptQuote(
                    float(con.strike), q.bid if q.bid is not None else 0.0, q.ask,
                    q.bid_size, q.ask_size,
                )
            )  # fmt: skip
        return out
