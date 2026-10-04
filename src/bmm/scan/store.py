"""Parquet store: data/scan/<table>/<YYYY-MM-DD>/<HHMMSS>.parquet (UTC)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

DEFAULT_ROOT = Path("data/scan")


def write(table: str, rows: list[dict[str, Any]], ts_ns: int, root: Path = DEFAULT_ROOT) -> Path:
    t = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC)
    path = root / table / t.strftime("%Y-%m-%d") / f"{t.strftime('%H%M%S')}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, infer_schema_length=None).write_parquet(path)
    return path


def read(table: str, date: str | None = None, root: Path = DEFAULT_ROOT) -> pl.DataFrame:
    base = root / table
    pattern = f"{date}/*.parquet" if date else "*/*.parquet"
    files = sorted(base.glob(pattern))
    if not files:
        return pl.DataFrame()
    return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
