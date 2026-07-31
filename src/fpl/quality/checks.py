"""Wires the generic quality-gate framework to the concrete staged tables.

Spec §10: `fpl check` is the boundary a CI job actually calls. Table-specific
gate sets live here so :mod:`fpl.quality.gates` stays generic and reusable by
the fact layer in phase 5.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.quality.gates import (
    Gate,
    Violation,
    enum_values,
    in_range,
    non_negative,
    run_gates,
    unique_key,
)
from fpl.storage import paths

__all__ = ["STAGED_TABLE_GATES", "check_staged_table", "check_staged_tables"]


STAGED_TABLE_GATES: dict[str, list[Gate]] = {
    "players": [
        unique_key(["player_id"]),
        enum_values("element_type", [1, 2, 3, 4]),
        non_negative("now_cost"),
        non_negative("minutes"),
    ],
    "teams": [unique_key(["team_id"])],
    "events": [unique_key(["event"])],
    "fixtures": [
        unique_key(["fixture_id"]),
        in_range("minutes", minimum=0, maximum=120),
    ],
    "price_snapshots": [unique_key(["player_id", "as_of_ts"])],
    "availability_snapshots": [unique_key(["player_id", "as_of_ts"])],
    "entry_snapshots": [unique_key(["entry_id", "as_of_ts"])],
    "player_fixture_stats": [
        unique_key(["player_id", "fixture_id"]),
        in_range("minutes", minimum=0, maximum=120),
        non_negative("goals_scored"),
    ],
}


def check_staged_table(
    table: str, season: Season, *, data_root: Path | None = None
) -> list[Violation]:
    """Run the declared gates for one staged table. Empty if the table is absent."""
    gates = STAGED_TABLE_GATES.get(table)
    if not gates:
        return []
    directory = paths.staged_table(table, season, data_root=data_root)
    part = directory / "part.parquet"
    if not part.is_file():
        return []
    frame = pl.read_parquet(part)
    return run_gates(frame, gates)


def check_staged_tables(season: Season, *, data_root: Path | None = None) -> list[Violation]:
    violations: list[Violation] = []
    for table in STAGED_TABLE_GATES:
        violations.extend(check_staged_table(table, season, data_root=data_root))
    return violations
