"""Deterministic Parquet reads and writes.

Spec §11 requires that two runs over identical input produce byte-identical
output. That is not a purity fetish: it is what makes a Git diff meaningful.
Once every unchanged table is genuinely unchanged, any diff at all is a real
change worth looking at.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path

import polars as pl

from fpl.storage.atomic import atomic_write_bytes

__all__ = ["read_parquet", "write_parquet"]

_COMPRESSION = "zstd"
_COMPRESSION_LEVEL = 3


def write_parquet(
    frame: pl.DataFrame,
    path: Path,
    *,
    sort_by: Sequence[str] | None = None,
) -> None:
    """Write ``frame`` to ``path`` atomically and deterministically.

    ``sort_by`` should be the table's natural key. Supplying it makes the output
    independent of the order rows happened to arrive in, which is what allows a
    rebuild from an unchanged source to produce an unchanged file.

    Column order is the frame's own and is not rearranged — callers construct
    their frames deterministically, and alphabetising would make every table
    harder to read for no gain in stability.
    """
    if sort_by:
        missing = [column for column in sort_by if column not in frame.columns]
        if missing:
            raise KeyError(f"cannot sort by absent column(s): {missing}")
        frame = frame.sort(sort_by)

    buffer = io.BytesIO()
    frame.write_parquet(
        buffer,
        compression=_COMPRESSION,
        compression_level=_COMPRESSION_LEVEL,
        statistics=True,
    )
    atomic_write_bytes(path, buffer.getvalue())


def read_parquet(path: Path) -> pl.DataFrame:
    return pl.read_parquet(path)
