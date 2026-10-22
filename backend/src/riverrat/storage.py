"""Local Parquet files (the laptop-side history lake, DESIGN.md §6.6)."""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def write_parquet(path: Path, table: pa.Table) -> None:
    """Write atomically, so an interrupted job never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)
