from datetime import date

import polars as pl
import pytest

from bmm.spotcheck.schema import (
    FUTURES_SCHEMA,
    LADDER_SCHEMA,
    OPTIONS_SCHEMA,
    ParquetSink,
)

GOLDEN_LADDER = {
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
GOLDEN_FUTURES = {
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
GOLDEN_OPTIONS = {
    "snapshot_id": pl.String,
    "ts_ns": pl.Int64,
    "product": pl.String,
    "underlying_con_id": pl.Int64,
    "expiry_ts_ns": pl.Int64,
    "expiry_role": pl.String,
    "expiry_mismatch": pl.Boolean,
    "strike": pl.Float64,
    "call_bid": pl.Float64,
    "call_ask": pl.Float64,
    "und_px": pl.Float64,
    "atm_sigma": pl.Float64,
}


def test_golden_schemas():
    assert pl.DataFrame(schema=LADDER_SCHEMA).schema == pl.Schema(GOLDEN_LADDER)
    assert pl.DataFrame(schema=FUTURES_SCHEMA).schema == pl.Schema(GOLDEN_FUTURES)
    assert pl.DataFrame(schema=OPTIONS_SCHEMA).schema == pl.Schema(GOLDEN_OPTIONS)
    assert list(LADDER_SCHEMA) == list(GOLDEN_LADDER)  # column order too


def _ladder_row(i: int = 0) -> dict:
    return {
        "snapshot_id": f"s{i}", "ts_ns": 1 + i, "product": "forecastex-spx",
        "question_date": date(2026, 10, 5), "fix_ts_ns": 2, "strike": 6500.0,
        "yes_bid": 0.47, "yes_ask": None, "bid_size": 30, "ask_size": 25, "crossed": False,
        "raw_yes_bid": 0.47, "raw_yes_bid_size": 30, "raw_yes_ask": None, "raw_yes_ask_size": 0,
        "raw_no_bid": 0.5, "raw_no_bid_size": 25, "raw_no_ask": None, "raw_no_ask_size": 0,
        "last": None, "last_size": 0, "volume": None, "payout_scale": 1.0,
    }  # fmt: skip


def test_append_twice_roundtrip(tmp_path):
    sink = ParquetSink(tmp_path)
    day = date(2026, 10, 5)
    sink.append(day, "ladder", [_ladder_row(0), _ladder_row(1)])
    path = sink.append(day, "ladder", [_ladder_row(2), _ladder_row(3)])
    assert path == tmp_path / "date=2026-10-05" / "ladder.parquet"
    df = pl.read_parquet(path)
    assert df.height == 4
    assert df.schema == pl.Schema(LADDER_SCHEMA)
    assert df["yes_ask"].null_count() == 4


def test_append_missing_column_raises(tmp_path):
    row = _ladder_row()
    del row["strike"]
    with pytest.raises(ValueError):
        ParquetSink(tmp_path).append(date(2026, 10, 5), "ladder", [row])


def test_append_extra_column_raises(tmp_path):
    row = {**_ladder_row(), "bogus": 1}
    with pytest.raises(ValueError):
        ParquetSink(tmp_path).append(date(2026, 10, 5), "ladder", [row])


def test_no_tmp_files_left(tmp_path):
    sink = ParquetSink(tmp_path)
    sink.append(date(2026, 10, 5), "ladder", [_ladder_row()])
    sink.append(date(2026, 10, 5), "ladder", [_ladder_row(1)])
    assert list(tmp_path.rglob("*.tmp")) == []


def test_write_contracts_merges_per_product(tmp_path):
    import json

    sink = ParquetSink(tmp_path)
    day = date(2026, 10, 5)
    sink.write_contracts(day, "a", [{"conId": 1}])
    p = sink.write_contracts(day, "b", [{"conId": 2}])
    sink.write_contracts(day, "a", [{"conId": 3}])
    assert p == tmp_path / "date=2026-10-05" / "contracts.json"
    assert json.loads(p.read_text()) == {"a": [{"conId": 3}], "b": [{"conId": 2}]}
