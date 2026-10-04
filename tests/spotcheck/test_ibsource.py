import asyncio
import math
import re
from datetime import date
from types import SimpleNamespace as NS
from typing import ClassVar

import pytest
from ib_async import IB, Contract, ContractDetails

from bmm.spotcheck.contracts import PRODUCTS, OptExpiry
from bmm.spotcheck.ibsource import IBSource, disarm
from bmm.spotcheck.normalise import raw_quote
from bmm.spotcheck.safety import SafetyError
from bmm.spotcheck.source import FutQuote, SourceError

QD = date(2026, 10, 5)
NAN = math.nan


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    def placeOrder(self, *a): ...
    def cancelOrder(self, *a): ...
    def reqGlobalCancel(self): ...
    def reqIds(self, n): ...


class FakeIB:
    instances = 0
    log: ClassVar[list[str]] = []

    def __init__(self):
        type(self).instances += 1
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "ticker_batches", [])
        object.__setattr__(self, "accounts", ["DU1234567"])
        object.__setattr__(self, "details_by_type", {})
        object.__setattr__(self, "ticker_fn", lambda c: NS(bid=0.4, ask=0.5, bidSize=10, askSize=10,
                                                           last=NAN, lastSize=NAN, volume=NAN))
        object.__setattr__(self, "client", FakeClient())

    def __getattribute__(self, name):
        if not name.startswith("__") and name not in ("instances", "log"):
            type(self).log.append(name)
        return object.__getattribute__(self, name)

    def placeOrder(self, *a): ...
    def cancelOrder(self, *a): ...
    def reqGlobalCancel(self): ...
    def whatIfOrder(self, *a): ...
    def bracketOrder(self, *a): ...

    async def connectAsync(self, host, port, clientId, readonly):
        self.calls.append(("connectAsync", host, port, clientId, readonly))

    def managedAccounts(self):
        return self.accounts

    def disconnect(self):
        self.calls.append(("disconnect",))

    async def reqContractDetailsAsync(self, c):
        self.calls.append(("details", c.secType, c.exchange, c.symbol))
        return self.details_by_type.get(c.secType or "FUT", [])  # conId-only lookups are futures

    async def reqTickersAsync(self, *cs):
        self.ticker_batches.append(len(cs))
        return [self.ticker_fn(c) for c in cs]

    async def qualifyContractsAsync(self, *cs):
        for i, c in enumerate(cs):
            c.conId = 9000 + i
        return list(cs)

    async def reqSecDefOptParamsAsync(self, *a):
        self.calls.append(("secdef",) + a)
        return []


@pytest.fixture(autouse=True)
def _reset():
    FakeIB.instances = 0
    FakeIB.log = []


def cd(strike, right, long_name="", under=0, sec="OPT", **kw):
    c = Contract(secType=sec, conId=int(strike) * 10 + (1 if right == "C" else 2), strike=strike,
                 right=right, localSymbol=f"L{strike}{right}", multiplier="1", **kw)
    return ContractDetails(contract=c, minTick=0.01, priceMagnifier=1, longName=long_name,
                           lastTradeTime="15:00:00", timeZoneId="US/Central", underConId=under)


def src(ib, max_lines=90):
    return IBSource(ib, max_lines=max_lines)


# --- connect guards --------------------------------------------------------------------------
@pytest.mark.parametrize("port", [4001, 7496, 7497, 4003])
def test_bad_port_refused_before_ib_is_built(port):
    with pytest.raises(SafetyError):
        run(IBSource.connect("127.0.0.1", port, 7, ib_factory=FakeIB))
    assert FakeIB.instances == 0


def test_remote_host_refused_before_ib_is_built():
    with pytest.raises(SafetyError):
        run(IBSource.connect("10.1.2.3", 4002, 7, ib_factory=FakeIB))
    assert FakeIB.instances == 0
    s = run(IBSource.connect("10.1.2.3", 4002, 7, ib_factory=FakeIB, allow_hosts=["10.1.2.3"]))
    assert s is not None


def test_connect_is_readonly_on_4002():
    made = []

    def factory():
        made.append(FakeIB())
        return made[-1]

    run(IBSource.connect("127.0.0.1", 4002, 7, ib_factory=factory))
    assert ("connectAsync", "127.0.0.1", 4002, 7, True) in made[0].calls


def test_non_paper_account_disconnects_and_raises():
    made = []

    def factory():
        ib = FakeIB()
        object.__setattr__(ib, "accounts", ["DU1", "U9999999"])
        made.append(ib)
        return ib

    with pytest.raises(SafetyError):
        run(IBSource.connect("127.0.0.1", 4002, 7, ib_factory=factory))
    assert ("disconnect",) in made[0].calls


def test_no_order_names_touched_on_normal_flow():
    ib = FakeIB()
    ib.details_by_type["OPT"] = [cd(6500.0, "C"), cd(6500.0, "P")]
    s = run(IBSource.connect("127.0.0.1", 4002, 7, ib_factory=lambda: ib))
    run(s.ladder(PRODUCTS["forecastex-spx"], QD))
    run(s.close())
    assert not [n for n in FakeIB.log if re.search("order", n, re.IGNORECASE)], FakeIB.log
    allowed = {"calls", "ticker_batches", "accounts", "details_by_type", "ticker_fn", "client",
               "connectAsync", "managedAccounts", "disconnect", "reqContractDetailsAsync",
               "reqTickersAsync", "qualifyContractsAsync", "reqSecDefOptParamsAsync"}
    assert set(FakeIB.log) <= allowed, set(FakeIB.log) - allowed


# --- order stubs -------------------------------------------------------------------------------
NAMES = ["placeOrder", "cancelOrder", "reqGlobalCancel", "whatIfOrder", "whatIfOrderAsync",
         "bracketOrder", "oneCancelsAll", "exerciseOptions"]


def test_order_methods_raise_on_fake():
    ib = FakeIB()
    done = disarm(ib)
    for name in ("placeOrder", "cancelOrder", "reqGlobalCancel", "whatIfOrder", "bracketOrder"):
        with pytest.raises(SafetyError):
            object.__getattribute__(ib, name)()
        assert f"ib.{name}" in done
    for name in ("placeOrder", "cancelOrder", "reqGlobalCancel"):
        with pytest.raises(SafetyError):
            getattr(ib.client, name)()
    ib.client.reqIds(1)  # unrelated methods untouched


@pytest.mark.parametrize("name", NAMES)
def test_order_methods_raise_on_real_ib_async(name):
    ib = IB()  # constructed, never connected
    real = {n for n in NAMES if hasattr(ib, n)}
    client_real = {n for n in NAMES if hasattr(ib.client, n)}
    disarm(ib)
    if name in real:
        with pytest.raises(SafetyError):
            getattr(ib, name)()
    if name in client_real:
        with pytest.raises(SafetyError):
            getattr(ib.client, name)()
    assert {"placeOrder", "cancelOrder", "reqGlobalCancel"} <= real | client_real


# --- ladder ------------------------------------------------------------------------------------
def ladder_ib():
    ib = FakeIB()
    ib.details_by_type["OPT"] = [cd(k, r) for k in (6490.0, 6500.0, 6510.0) for r in "CP"]
    return ib


def test_ladder_six_quotes_and_details_log():
    ib = ladder_ib()
    quotes, log = run(src(ib).ladder(PRODUCTS["forecastex-spx"], QD))
    assert len(quotes) == 6 and sorted({q.right for q in quotes}) == ["C", "P"]
    assert quotes[0].quote.bid == 0.4 and quotes[0].quote.ask_size == 10
    assert set(log[0]) == {"conId", "localSymbol", "right", "strike", "multiplier", "minTick",
                           "priceMagnifier", "longName", "lastTradeTime", "timeZoneId", "underConId"}
    assert ("details", "OPT", "FORECASTX", "FES") in ib.calls


def test_ladder_batches_tickers():
    ib = ladder_ib()
    run(src(ib, max_lines=4).ladder(PRODUCTS["forecastex-spx"], QD))
    assert ib.ticker_batches == [4, 2]


def test_ladder_all_nan_raises_paper_error():
    ib = ladder_ib()
    object.__setattr__(ib, "ticker_fn", lambda c: NS(bid=NAN, ask=-1.0, bidSize=NAN, askSize=0,
                                                     last=NAN, lastSize=NAN, volume=NAN))
    with pytest.raises(SourceError, match="paper"):
        run(src(ib).ladder(PRODUCTS["forecastex-spx"], QD))


def test_ladder_empty_raises():
    with pytest.raises(SourceError, match="no ladder listed"):
        run(src(FakeIB()).ladder(PRODUCTS["forecastex-spx"], QD))


def test_cme_ladder_fetches_calls_and_puts():
    ib = FakeIB()
    ib.details_by_type["FOP"] = [cd(4000.0, "C", sec="FOP"), cd(4000.0, "P", sec="FOP")]
    quotes, _ = run(src(ib).ladder(PRODUCTS["cme-gold"], QD))
    assert sorted(q.right for q in quotes) == ["C", "P"]
    assert ("details", "FOP", "COMEX", "GC") in ib.calls


# --- reference future --------------------------------------------------------------------------
def fut_cd(month, real_exp="", con=5000):
    c = Contract(secType="FUT", conId=con, localSymbol="GCZ6", lastTradeDateOrContractMonth=month)
    return ContractDetails(contract=c, contractMonth=month, realExpirationDate=real_exp)


def test_named_gc_resolves_from_long_name_and_gc_month():
    ib = FakeIB()
    ib.details_by_type["FUT"] = [fut_cd("202612")]
    spec = PRODUCTS["forecastex-gold"]
    f = run(src(ib).reference_future(spec, QD, [{"longName": "Gold Dec 2026"}], None))
    assert f.contract_month == "202612" and f.con_id == 5000 and f.quote.bid == 0.4
    f2 = run(src(ib).reference_future(spec, QD, [{"longName": "no month here"}], "202612"))
    assert f2.contract_month == "202612"
    with pytest.raises(SourceError, match="gc-month"):
        run(src(ib).reference_future(spec, QD, [{"longName": "nothing"}], None))


def test_lead_quarterly_and_under_con_id():
    ib = FakeIB()
    ib.details_by_type["FUT"] = [fut_cd("202612", "20261218", 1), fut_cd("202703", "20270319", 2)]
    f = run(src(ib).reference_future(PRODUCTS["forecastex-spx"], QD, [], None))
    assert f.con_id == 1
    f = run(src(ib).reference_future(PRODUCTS["forecastex-spx"], date(2026, 12, 18), [], None))
    assert f.con_id == 2  # rolls on final settlement day
    ib.details_by_type["FUT"] = [fut_cd("202612", "", 77)]
    f = run(src(ib).reference_future(PRODUCTS["cme-gold"], QD, [{"underConId": 77}], None))
    assert f.con_id == 77
    with pytest.raises(SourceError):
        run(src(ib).reference_future(PRODUCTS["cme-gold"], QD, [{"underConId": 0}], None))


def test_expiries_strikes_and_calls(monkeypatch):
    from bmm.clock import ct_to_ns

    monkeypatch.setattr("bmm.spotcheck.ibsource.now_ns", lambda: ct_to_ns(QD, 9, 0))
    ib = FakeIB()
    chain = NS(exchange="CME", tradingClass="ES", expirations={"20261005", "20261201"},
               strikes={6500.0, 6510.0, 6520.0})

    async def secdef(*a):
        return [chain]

    object.__setattr__(ib, "reqSecDefOptParamsAsync", secdef)
    ib.details_by_type["FOP"] = [cd(6510.0, "C", under=1, sec="FOP")]
    s = src(ib)
    spec = PRODUCTS["forecastex-spx"]
    fut = FutQuote(1, "ESZ6", "202612", raw_quote(None, None, 0, 0))
    exps = run(s.option_expiries(spec, fut))
    assert exps == [OptExpiry(ct_to_ns(QD, 15, 0), 1, "ES", "20261005")]  # 20261201 outside 7d
    assert run(s.listed_strikes(spec, exps[0])) == [6500.0, 6510.0, 6520.0]
    qs = run(s.calls(spec, exps[0], [6500.0, 6510.0]))
    assert [q.strike for q in qs] == [6500.0, 6510.0] and qs[0].call_ask == 0.5
