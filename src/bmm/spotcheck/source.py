"""Market-data source protocol shared by the IB adapter and the synthetic fixture."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol

from bmm.spotcheck.analyze import OptQuote
from bmm.spotcheck.contracts import OptExpiry, ProductSpec
from bmm.spotcheck.normalise import RawQuote


class SourceError(RuntimeError):
    """Product-level failure: no data, no subscription, scale problem, unresolved month."""


@dataclass(frozen=True)
class LadderQuote:
    strike: float
    right: Literal["C", "P"]  # ForecastEx: YES=C, NO=P. CME: call book and put book
    quote: RawQuote


@dataclass(frozen=True)
class FutQuote:
    con_id: int
    local_symbol: str
    contract_month: str
    quote: RawQuote


class MarketDataSource(Protocol):
    async def ladder(
        self, spec: ProductSpec, question_date: date
    ) -> tuple[list[LadderQuote], list[dict[str, Any]]]:
        """Venue ladder quotes plus the contract-details log."""
        ...

    async def reference_future(
        self,
        spec: ProductSpec,
        question_date: date,
        ladder_details: list[dict[str, Any]],
        gc_month: str | None,
    ) -> FutQuote: ...

    async def option_expiries(self, spec: ProductSpec, fut: FutQuote) -> list[OptExpiry]: ...

    async def calls(
        self, spec: ProductSpec, expiry: OptExpiry, strikes: list[float]
    ) -> list[OptQuote]: ...

    async def listed_strikes(self, spec: ProductSpec, expiry: OptExpiry) -> list[float]: ...

    async def close(self) -> None: ...
