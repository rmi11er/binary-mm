"""Parquet schemas and an atomic append-only sink for spotcheck snapshots."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl

LADDER_SCHEMA: dict[str, Any] = {
    "snapshot_id": pl.String,
    "ts_ns": pl.Int64,
    "product": pl.String,
    "question_date": pl.Date,
    "fix_ts_ns": pl.Int64,
    "strike": pl.Float64,
    "yes_bid": pl.Float64,
    "yes_ask": pl.Float64,
    "bid_size": pl.Int64,
    "ask_size": pl.Int64,
    "crossed": pl.Boolean,
    "raw_yes_bid": pl.Float64,
    "raw_yes_bid_size": pl.Int64,
    "raw_yes_ask": pl.Float64,
    "raw_yes_ask_size": pl.Int64,
    "raw_no_bid": pl.Float64,
    "raw_no_bid_size": pl.Int64,
    "raw_no_ask": pl.Float64,
    "raw_no_ask_size": pl.Int64,
    "last": pl.Float64,
    "last_size": pl.Int64,
    "volume": pl.Float64,
    "payout_scale": pl.Float64,
}

FUTURES_SCHEMA: dict[str, Any] = {
    "snapshot_id": pl.String,
    "ts_ns": pl.Int64,
    "product": pl.String,
    "con_id": pl.Int64,
    "local_symbol": pl.String,
    "contract_month": pl.String,
    "bid": pl.Float64,
    "ask": pl.Float64,
    "bid_size": pl.Int64,
    "ask_size": pl.Int64,
    "last": pl.Float64,
}

OPTIONS_SCHEMA: dict[str, Any] = {
    "snapshot_id": pl.String,
    "ts_ns": pl.Int64,
    "product": pl.String,
    "underlying_con_id": pl.Int64,
    "expiry_ts_ns": pl.Int64,
    "expiry_role": pl.String,  # "match" | "below" | "above"
    "expiry_mismatch": pl.Boolean,
    "strike": pl.Float64,
    "call_bid": pl.Float64,
    "call_ask": pl.Float64,
    "und_px": pl.Float64,
    "atm_sigma": pl.Float64,
}

_SCHEMAS: dict[str, dict[str, Any]] = {
    "ladder": LADDER_SCHEMA,
    "futures": FUTURES_SCHEMA,
    "options": OPTIONS_SCHEMA,
}


def _atomic_write(path: Path, write: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    write(tmp)
    os.replace(tmp, path)


class ParquetSink:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _day_dir(self, day: date) -> Path:
        d = self.root / f"date={day.isoformat()}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def append(
        self,
        day: date,
        table: Literal["ladder", "futures", "options"],
        rows: list[dict[str, Any]],
    ) -> Path:
        schema = _SCHEMAS[table]
        for r in rows:
            if set(r) != set(schema):
                missing, extra = set(schema) - set(r), set(r) - set(schema)
                raise ValueError(f"{table} row columns mismatch: missing={missing} extra={extra}")
        path = self._day_dir(day) / f"{table}.parquet"
        df = pl.DataFrame(rows, schema=schema)
        if path.exists():
            df = pl.concat([pl.read_parquet(path), df])
        _atomic_write(path, df.write_parquet)
        return path

    def write_contracts(self, day: date, product: str, details: list[dict[str, Any]]) -> Path:
        path = self._day_dir(day) / "contracts.json"
        data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
        data[product] = details  # replaces any earlier entry for this product
        _atomic_write(path, lambda t: t.write_text(json.dumps(data, indent=2, default=str)))
        return path
