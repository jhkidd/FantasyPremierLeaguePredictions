"""Data quality gates: pure checks run as a boundary between pipeline layers.

Spec §10: quality gates block the commit rather than letting bad data reach
``main``. A gate is a named pure function ``frame -> list[Violation]``, and
``run_gates`` runs every gate rather than stopping at the first failure — a
report naming every problem found is worth more than one that stops at the
first, especially when the report is read once, days later, in a CI log.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import polars as pl

__all__ = [
    "Gate",
    "Severity",
    "Violation",
    "enum_values",
    "has_blocking_violations",
    "in_range",
    "non_negative",
    "not_null",
    "referential",
    "run_gates",
    "unique_key",
]

Severity = Literal["block", "warn"]

_SAMPLE_SIZE = 5


@dataclass(frozen=True)
class Violation:
    """One gate's finding against one frame.

    ``sample`` carries a handful of offending rows so a failure is diagnosable
    from the log alone — a bare count of "412 rows failed" cannot be triaged
    without re-running the pipeline locally.
    """

    gate: str
    detail: str
    severity: Severity
    rows: int
    sample: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Gate:
    name: str
    check: Callable[[pl.DataFrame], list[Violation]]


def run_gates(frame: pl.DataFrame, gates: Sequence[Gate]) -> list[Violation]:
    """Run every gate and aggregate their violations.

    Deliberately does not short-circuit: seeing every problem in one run is
    what makes a quality-gate failure something a person can act on rather
    than something they have to re-run three more times to fully understand.
    """
    violations: list[Violation] = []
    for gate in gates:
        violations.extend(gate.check(frame))
    return violations


def has_blocking_violations(violations: Sequence[Violation]) -> bool:
    return any(violation.severity == "block" for violation in violations)


def _sample(frame: pl.DataFrame) -> tuple[dict[str, Any], ...]:
    return tuple(frame.head(_SAMPLE_SIZE).to_dicts())


def unique_key(columns: Sequence[str], *, severity: Severity = "block") -> Gate:
    """Every row is unique on ``columns`` — the fact layer's primary key."""
    cols = list(columns)
    name = f"unique_key({cols})"

    def check(frame: pl.DataFrame) -> list[Violation]:
        missing = [c for c in cols if c not in frame.columns]
        if missing:
            return [Violation(name, f"missing key column(s): {missing}", severity, 0)]
        duplicated = frame.filter(pl.struct(cols).is_duplicated())
        if duplicated.height == 0:
            return []
        return [
            Violation(
                name,
                f"{duplicated.height} row(s) duplicate the key {cols}",
                severity,
                duplicated.height,
                _sample(duplicated),
            )
        ]

    return Gate(name, check)


def not_null(column: str, *, severity: Severity = "block") -> Gate:
    name = f"not_null({column})"

    def check(frame: pl.DataFrame) -> list[Violation]:
        if column not in frame.columns:
            return [Violation(name, f"column {column!r} absent", severity, 0)]
        bad = frame.filter(pl.col(column).is_null())
        if bad.height == 0:
            return []
        return [
            Violation(
                name,
                f"{bad.height} null value(s) in {column!r}",
                severity,
                bad.height,
                _sample(bad),
            )
        ]

    return Gate(name, check)


def in_range(
    column: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    severity: Severity = "block",
) -> Gate:
    """Non-null values of ``column`` fall within ``[minimum, maximum]``."""
    name = f"in_range({column}, {minimum}, {maximum})"

    def check(frame: pl.DataFrame) -> list[Violation]:
        if column not in frame.columns:
            return [Violation(name, f"column {column!r} absent", severity, 0)]
        conditions = []
        if minimum is not None:
            conditions.append(pl.col(column) < minimum)
        if maximum is not None:
            conditions.append(pl.col(column) > maximum)
        if not conditions:
            return []
        out_of_range = conditions[0]
        for extra in conditions[1:]:
            out_of_range = out_of_range | extra
        bad = frame.filter(pl.col(column).is_not_null() & out_of_range)
        if bad.height == 0:
            return []
        return [
            Violation(
                name,
                f"{bad.height} row(s) of {column!r} outside [{minimum}, {maximum}]",
                severity,
                bad.height,
                _sample(bad),
            )
        ]

    return Gate(name, check)


def non_negative(column: str, *, severity: Severity = "block") -> Gate:
    return in_range(column, minimum=0, severity=severity)


def enum_values(column: str, allowed: Sequence[Any], *, severity: Severity = "block") -> Gate:
    """Non-null values of ``column`` are all members of ``allowed``."""
    allowed_list = list(allowed)
    name = f"enum_values({column})"

    def check(frame: pl.DataFrame) -> list[Violation]:
        if column not in frame.columns:
            return [Violation(name, f"column {column!r} absent", severity, 0)]
        bad = frame.filter(pl.col(column).is_not_null() & ~pl.col(column).is_in(allowed_list))
        if bad.height == 0:
            return []
        seen = bad.select(column).unique().to_series().to_list()
        return [
            Violation(
                name,
                f"{bad.height} row(s) with unexpected {column!r}: {seen[:10]}",
                severity,
                bad.height,
                _sample(bad),
            )
        ]

    return Gate(name, check)


def referential(
    column: str,
    reference: pl.DataFrame,
    reference_column: str,
    *,
    severity: Severity = "block",
) -> Gate:
    """Every non-null value of ``column`` appears in ``reference[reference_column]``."""
    name = f"referential({column} -> {reference_column})"

    def check(frame: pl.DataFrame) -> list[Violation]:
        if column not in frame.columns:
            return [Violation(name, f"column {column!r} absent", severity, 0)]
        known = reference.select(pl.col(reference_column)).to_series()
        bad = frame.filter(pl.col(column).is_not_null() & ~pl.col(column).is_in(known))
        if bad.height == 0:
            return []
        return [
            Violation(
                name,
                f"{bad.height} row(s) of {column!r} not present in {reference_column!r}",
                severity,
                bad.height,
                _sample(bad),
            )
        ]

    return Gate(name, check)
