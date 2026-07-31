"""Framework for turning raw payloads into typed, declared tables.

Spec §6: raw -> staged is where every source-specific quirk is quarantined. An
unknown incoming column is a warning; a missing expected column is a failure.
That asymmetry is deliberate — a source *adding* a field is normal and must
not stop the pipeline, but *removing* one silently changes what every
downstream consumer assumes is present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl

from fpl.sources.errors import SchemaError

__all__ = ["ColumnSpec", "StagingReport", "TableSpec", "stage_frame"]


@dataclass(frozen=True)
class ColumnSpec:
    """One output column: what to call it, where it comes from, and its type.

    ``group`` tags which availability-mask group this column belongs to (spec
    §4) — e.g. ``"defensive"`` for CBI/tackles/recoveries. Facts assembly reads
    it to decide which mask boolean a missing column should clear.
    """

    name: str
    source_name: str
    dtype: pl.PolarsDataType
    required: bool = True
    group: str = "core"


@dataclass(frozen=True)
class TableSpec:
    """The declared shape of one staged table, for one source era."""

    table: str
    columns: tuple[ColumnSpec, ...]
    key: tuple[str, ...]
    encoding: str = "utf-8"
    drop: frozenset[str] = field(default_factory=frozenset)
    """Fields the spec says never to import, even when present (spec §7):
    ``ep_next``, ``form``, ``xP`` and anything else FPL overwrites in place."""

    def __post_init__(self) -> None:
        names = [column.name for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.table}: duplicate output column name(s) in spec")
        missing_key = [k for k in self.key if k not in names]
        if missing_key:
            raise ValueError(f"{self.table}: key column(s) not declared as output: {missing_key}")


@dataclass(frozen=True)
class StagingReport:
    table: str
    rows_in: int
    rows_out: int
    unknown_columns: tuple[str, ...]
    excluded: dict[str, int] = field(default_factory=dict)
    """Rows dropped for a table-specific reason (e.g. manager assets), and why.
    Logged and asserted rather than silently absorbed (spec plan §4.6/6.1)."""


def stage_frame(
    raw: pl.DataFrame,
    spec: TableSpec,
    *,
    excluded: dict[str, int] | None = None,
) -> tuple[pl.DataFrame, StagingReport]:
    """Rename, cast and select ``raw`` according to ``spec``.

    Any declared column absent from ``raw`` but not ``required`` is written as
    a typed null — this is how an availability tier that a given era never
    exposed (e.g. defensive-contribution inputs in 2019/20) still produces a
    row with a well-typed absence, rather than a `KeyError` downstream.
    """
    rows_in = raw.height
    declared_source_names = {column.source_name for column in spec.columns}

    unknown = tuple(
        sorted(
            name
            for name in raw.columns
            if name not in declared_source_names and name not in spec.drop
        )
    )

    missing_required = [
        column.source_name
        for column in spec.columns
        if column.required and column.source_name not in raw.columns
    ]
    if missing_required:
        raise SchemaError(f"{spec.table}: missing required source column(s): {missing_required}")

    selections: list[pl.Expr] = []
    for column in spec.columns:
        if column.source_name in raw.columns:
            selections.append(
                pl.col(column.source_name).cast(column.dtype, strict=False).alias(column.name)
            )
        else:
            selections.append(pl.lit(None, dtype=column.dtype).alias(column.name))
    staged = raw.select(selections)

    report = StagingReport(
        table=spec.table,
        rows_in=rows_in,
        rows_out=staged.height,
        unknown_columns=unknown,
        excluded=dict(excluded or {}),
    )
    return staged, report


def decode_csv(body: bytes, encoding: str) -> pl.DataFrame:
    """Decode CSV bytes using a **declared** encoding, never a sniffed one.

    Two of the archive's schema eras (2016/17, 2017/18) are cp1252, not UTF-8.
    Naive UTF-8 decoding either raises or silently mojibakes accented names
    (``Bešić`` -> ``BeÅ¡iÄ‡``), and mojibake is the worse failure because it
    survives into a committed parquet file undetected.
    """
    import io

    text = body.decode(encoding)
    return pl.read_csv(io.BytesIO(text.encode("utf-8")), infer_schema_length=None)


def as_str_or_none(value: Any) -> str | None:
    """Normalise polars/CSV-ish blanks to ``None`` before casting."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
