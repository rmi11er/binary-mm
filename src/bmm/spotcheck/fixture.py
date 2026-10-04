"""Deterministic synthetic session for dry runs and tests (no network, no IB).

Designed so the report yields known verdicts on question date 2026-10-05:
  forecastex-spx   GO            strikes in the 1-2 sigma bucket +-4c off fair, size 40, matched expiry
  forecastex-ndx   KILL          mids within +-1c of fair, 4c wide markets
  forecastex-gold  INVESTIGATE   same +-4c gaps, displayed size 3 (tiny)
  cme-gold         INVESTIGATE   same gaps, size 40, but the only option expiry is fix + 24h
                                 (every snapshot is expiry-mismatched)
Gaps are measured against the same `analyze.fair_band` the report uses, so the designed gap is
what the analysis sees. "Rich" means venue mid above fair on the upside and below fair on the
downside (|gap| 4-5c; a +4c premium is impossible on a 0.97 downside digital). Every product has
one crossed row, 2 trades after the fix (volume steps 100 -> 105 -> 110 at the centre strike) and
resting quotes after the fix.

    uv run python -m bmm.spotcheck.fixture tests/fixtures/spotcheck/session.json
"""

from __future__ import annotations

import json
import math
import sys
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from bmm.clock import ct_to_ns, ns_to_ct
from bmm.spotcheck.analyze import OptQuote, fair_band
from bmm.spotcheck.contracts import PRODUCTS, OptExpiry, ProductSpec
from bmm.spotcheck.normalise import raw_quote
from bmm.spotcheck.source import FutQuote, LadderQuote, SourceError
from bmm.spotcheck.vol import black76_call

MIN_NS = 60 * 1_000_000_000
YEAR_NS = 365 * 86400 * 1_000_000_000
OFFSETS_MIN = list(range(-60, -30, 5)) + list(range(-30, 31))  # 5-min then 1-min grid
SKEW = 0.8  # vol(K) = vol0 - SKEW * ln(K / F), as in test_analyze
CROSSED_AT_MIN, TRADES_AT_MIN = -24, (5, 12)
SIZE_CENTER_VOLUME = 100.0


@dataclass(frozen=True)
class _Params:
    con_id: int
    f0: float
    vol0: float
    drift: float
    tick: float
    ladder_step: float
    ladder_n: int
    opt_step: float
    opt_half: float
    opt_hs: float
    trading_class: str
    size: int
    venue_hs: float
    mode: str  # "rich" | "fair"
    noise: float
    expiry_offset_min: int  # option expiry relative to fix
    fut_symbol_month: str = "202612"


_P: dict[str, _Params] = {
    "forecastex-spx": _Params(1001, 6500.0, 0.16, 3.0, 0.25, 1.0, 30, 5.0, 130.0, 0.05, "ES", 40,
                              0.01, "rich", 0.002, 0),
    "forecastex-ndx": _Params(1002, 23000.0, 0.22, 12.0, 0.25, 5.0, 30, 25.0, 500.0, 0.5, "NQ", 40,
                              0.02, "fair", 0.01, 0),
    "forecastex-gold": _Params(1003, 4000.0, 0.18, 1.5, 0.1, 1.0, 30, 10.0, 150.0, 0.1, "OG", 3,
                               0.01, "rich", 0.002, 0),
    "cme-gold": _Params(1004, 4000.0, 0.18, 1.5, 0.1, 5.0, 20, 10.0, 200.0, 0.1, "ECGC", 40,
                        0.01, "rich", 0.002, 24 * 60),
}  # fmt: skip


@dataclass
class Snap:
    ts_ns: int
    fut: list[float | None]  # bid, ask, bid_size, ask_size, last
    # per ladder strike: yes_bid, yes_ask, bid_size, ask_size, last, last_size, volume
    ladder: list[list[float | None]]
    opts: dict[str, list[list[float]]] = field(default_factory=dict)  # key -> [bids, asks]


@dataclass
class ProductData:
    key: str
    fut_con_id: int
    fut_local_symbol: str
    fut_month: str
    details: list[dict[str, Any]]
    expiries: list[dict[str, Any]]
    ladder_strikes: list[float]
    opt_strikes: list[float]
    snaps: list[Snap]


@dataclass
class Session:
    question_date: str
    seed: int
    products: dict[str, ProductData]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Session:
        prods = {}
        for k, p in d["products"].items():
            snaps = [Snap(**s) for s in p["snaps"]]
            prods[k] = ProductData(**{**p, "snaps": snaps})
        return cls(d["question_date"], d["seed"], prods)


def _key(e: OptExpiry) -> str:
    return f"{e.underlying_con_id}:{e.trading_class}:{e.yyyymmdd}"


def _round_tick(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, 6)


def _digital_numeric(F: float, K: float, T: float, vol0: float) -> float:
    h = K * 1e-4

    def c(k: float) -> float:
        return black76_call(F, k, T, vol0 - SKEW * math.log(k / F))

    return min(max(-(c(K + h) - c(K - h)) / (2 * h), 0.005), 0.995)


def _build_product(key: str, qd: date, seed: int, idx: int) -> ProductData:
    p = _P[key]
    spec = PRODUCTS[key]
    fix = ct_to_ns(qd, *spec.fix_ct)
    expiry_ts = fix + p.expiry_offset_min * MIN_NS
    exp_date = ns_to_ct(expiry_ts).date().strftime("%Y%m%d")
    rng = np.random.default_rng(seed * 1000 + idx)
    f0 = _round_tick(p.f0, p.ladder_step)
    lad_k = [f0 + p.ladder_step * i for i in range(-p.ladder_n, p.ladder_n + 1)]
    opt_k = [f0 + p.opt_step * i for i in range(-int(p.opt_half / p.opt_step),
                                                 int(p.opt_half / p.opt_step) + 1)]  # fmt: skip
    centre = p.ladder_n  # index of the strike at f0
    expiry = OptExpiry(expiry_ts, p.con_id, p.trading_class, exp_date)
    expiries = [expiry]
    if key == "forecastex-spx":  # decoy chain on another future with the same expiry
        expiries.append(OptExpiry(expiry_ts, 9999, "ES", exp_date))
    ekey = _key(expiry)

    snaps: list[Snap] = []
    f_settle: float | None = None
    for i, off in enumerate(OFFSETS_MIN):
        ts = fix + off * MIN_NS
        if p.expiry_offset_min == 0 and ts >= fix:
            F = f_settle  # settled: the fix price is frozen
        else:
            raw = p.f0 + p.drift * math.sin(2 * math.pi * i / 23) + rng.normal(0, p.drift / 15)
            F = _round_tick(min(max(raw, p.f0 - p.drift), p.f0 + p.drift), p.tick)
        assert F is not None
        if off == -1:
            f_settle = F
        has_opts = ts < expiry_ts
        T = (expiry_ts - ts) / YEAR_NS if has_opts else 0.0

        opts: dict[str, list[list[float]]] = {}
        chain: list[OptQuote] = []
        if has_opts:
            bids, asks = [], []
            for k in opt_k:
                mid = black76_call(F, k, T, p.vol0 - SKEW * math.log(k / F))
                bids.append(round(max(mid - p.opt_hs, 0.0), 4))
                asks.append(round(mid + p.opt_hs, 4))
            opts[ekey] = [bids, asks]
            chain = [OptQuote(k, b, a) for k, b, a in zip(opt_k, bids, asks, strict=True)]

        rows: list[list[float | None]] = []
        trades_done = sum(1 for t in TRADES_AT_MIN if off >= t)
        for j, k in enumerate(lad_k):
            if has_opts:
                fb = fair_band(chain, k)
                fair = fb.fair if fb is not None else _digital_numeric(F, k, T, p.vol0)
                m = abs(math.log(k / F)) / (p.vol0 * math.sqrt(T))
                if p.mode == "rich" and 1.05 <= m < 1.95:
                    if k > F:
                        mid = math.ceil((fair + 0.04) * 100 - 1e-9) / 100
                    else:
                        mid = math.floor((fair - 0.04) * 100 + 1e-9) / 100
                else:
                    n = (rng.uniform(-p.noise, p.noise) if p.mode == "fair"
                         else float(np.clip(rng.normal(0, p.noise), -2 * p.noise, 2 * p.noise)))
                    mid = round(fair + n, 2)
                yb = min(max(round(mid - p.venue_hs, 2), 0.01), 0.98)
                ya = min(max(round(mid + p.venue_hs, 2), round(yb + 0.01, 2)), 0.99)
            else:  # settled: resting quotes either side of 0 / 1
                yb, ya = (0.98, 0.99) if k < F else (0.01, 0.02)
            if j == centre and off == CROSSED_AT_MIN:
                ya = round(yb - 0.01, 2)
            last: float | None = None
            last_size = 0.0
            vol: float | None = None
            if j == centre:
                vol = SIZE_CENTER_VOLUME + 5.0 * trades_done
                if trades_done:
                    last, last_size = yb, 5.0
            rows.append([yb, ya, float(p.size), float(p.size), last, last_size, vol])
        fut: list[float | None] = [round(F - p.tick / 2, 6), round(F + p.tick / 2, 6), 20.0, 20.0, F]
        snaps.append(Snap(ts, fut, rows, opts))

    month_name = "Dec 2026"
    details: list[dict[str, Any]] = []
    for k in lad_k:
        for r in ("C", "P"):
            details.append({
                "conId": int(k * 10) * 10 + (1 if r == "C" else 2) + p.con_id * 10**9,
                "localSymbol": f"{spec.ib_symbol} {k:g}{r}",
                "right": r, "strike": k, "multiplier": "1", "minTick": 0.01, "priceMagnifier": 1,
                "longName": f"{spec.ib_symbol} Gold Futures {month_name} above {k:g}"
                if "gold" in key else f"{spec.ib_symbol} {fix_name(spec)} above {k:g}",
                "lastTradeTime": f"{spec.fix_ct[0]:02d}:{spec.fix_ct[1]:02d}:00",
                "timeZoneId": "America/Chicago", "underConId": p.con_id,
            })  # fmt: skip
    return ProductData(
        key=key,
        fut_con_id=p.con_id,
        fut_local_symbol=f"{spec.fut_symbol}Z6",
        fut_month=p.fut_symbol_month,
        details=details,
        expiries=[asdict(e) for e in expiries],
        ladder_strikes=lad_k,
        opt_strikes=opt_k,
        snaps=snaps,
    )


def fix_name(spec: ProductSpec) -> str:
    return {"ES": "S&P 500 futures", "NQ": "Nasdaq-100 futures"}.get(spec.fut_symbol, "futures")


def make_session(question_date: date, seed: int = 7) -> Session:
    prods = {k: _build_product(k, question_date, seed, i) for i, k in enumerate(_P)}
    return Session(question_date.isoformat(), seed, prods)


class FixtureSource:
    """MarketDataSource over a Session. Serves the latest snapshot with ts <= clock()."""

    def __init__(self, session: Session, clock: Callable[[], int] | None = None) -> None:
        self.session = session
        self._clock: Callable[[], int] = clock or (lambda: 2**62)

    def set_clock(self, clock: Callable[[], int]) -> None:
        self._clock = clock

    @classmethod
    def from_json(cls, path: Path | str, clock: Callable[[], int] | None = None) -> FixtureSource:
        return cls(Session.from_dict(json.loads(Path(path).read_text())), clock)

    def to_json(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.session.to_dict(), separators=(",", ":")))

    def _p(self, spec: ProductSpec) -> ProductData:
        try:
            return self.session.products[spec.key]
        except KeyError:
            raise SourceError(f"fixture has no product {spec.key}") from None

    def _snap(self, spec: ProductSpec) -> tuple[ProductData, Snap]:
        p = self._p(spec)
        i = bisect_right([s.ts_ns for s in p.snaps], self._clock()) - 1
        if i < 0:
            raise SourceError("fixture: no snapshot yet")
        return p, p.snaps[i]

    async def ladder(
        self, spec: ProductSpec, question_date: date
    ) -> tuple[list[LadderQuote], list[dict[str, Any]]]:
        p, s = self._snap(spec)
        out: list[LadderQuote] = []
        for k, (yb, ya, bs, asz, last, lsz, vol) in zip(p.ladder_strikes, s.ladder, strict=True):
            assert yb is not None and ya is not None and bs is not None and asz is not None
            out.append(LadderQuote(k, "C", raw_quote(yb, ya, int(bs), int(asz), last, int(lsz or 0), vol)))
            out.append(LadderQuote(k, "P", raw_quote(1.0 - ya, 1.0 - yb, int(asz), int(bs))))
        return out, [dict(d) for d in p.details]

    async def reference_future(
        self,
        spec: ProductSpec,
        question_date: date,
        ladder_details: list[dict[str, Any]],
        gc_month: str | None,
    ) -> FutQuote:
        p, s = self._snap(spec)
        b, a, bs, asz, last = s.fut
        return FutQuote(
            p.fut_con_id, p.fut_local_symbol, p.fut_month,
            raw_quote(b, a, int(bs or 0), int(asz or 0), last),
        )  # fmt: skip

    async def option_expiries(self, spec: ProductSpec, fut: FutQuote) -> list[OptExpiry]:
        p, _ = self._snap(spec)
        now = self._clock()
        return [OptExpiry(**e) for e in p.expiries if e["expiry_ts_ns"] > now]

    async def listed_strikes(self, spec: ProductSpec, expiry: OptExpiry) -> list[float]:
        return list(self._p(spec).opt_strikes)

    async def calls(
        self, spec: ProductSpec, expiry: OptExpiry, strikes: list[float]
    ) -> list[OptQuote]:
        p, s = self._snap(spec)
        data = s.opts.get(_key(expiry))
        if data is None:
            raise SourceError(f"fixture: no option data for {_key(expiry)}")
        by_k = dict(zip(p.opt_strikes, zip(data[0], data[1], strict=True), strict=True))
        return [OptQuote(k, *by_k[k]) for k in strikes if k in by_k]

    async def close(self) -> None:
        return None


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    FixtureSource(make_session(date(2026, 10, 5))).to_json(out)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
