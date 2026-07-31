"""Stage vaastav's ``merged_gw.csv``, one schema era at a time.

Only era **E7** (2025/26) is implemented here — the resequencing decision in
the phases 4-6 plan moves the 2025/26 slice into phase 4 because phase 5's
reconciliation target can only come from vaastav. The other six eras (E1-E6)
are phase 6 work (Finding 5).
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from fpl.config import Season
from fpl.staging.base import ColumnSpec, StagingReport, TableSpec, decode_csv, stage_frame

__all__ = [
    "ERA_BY_SEASON",
    "MERGED_GW_SPECS",
    "StagedMergedGw",
    "era_for_season",
    "stage_merged_gw",
]

ERA_BY_SEASON: dict[Season, str] = {
    Season(2025): "E7",
}
"""Which schema era a season's ``merged_gw.csv`` belongs to (Finding 5).

Deliberately a closed map: a season absent here must raise rather than be
guessed at, because a new upstream season needs a person to classify its
schema before it can be trusted (spec plan §4.6)."""

_MANAGER_ASSET_POSITION = "AM"
"""How 2024/25's manager rows are marked in the archive (Finding 6). Inert for
E7, kept here because the exclusion rule belongs to the spec, not the era."""

_ENCODING_BY_ERA: dict[str, str] = {"E7": "utf-8"}

_E7_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("player_name", "name", pl.Utf8),
    ColumnSpec("position", "position", pl.Utf8),
    ColumnSpec("team", "team", pl.Utf8),
    ColumnSpec("player_id", "element", pl.Int64),
    ColumnSpec("fixture_id", "fixture", pl.Int64),
    ColumnSpec("event", "GW", pl.Int64),
    ColumnSpec("round", "round", pl.Int64, required=False),
    ColumnSpec("kickoff_time", "kickoff_time", pl.Utf8),
    ColumnSpec("was_home", "was_home", pl.Boolean),
    ColumnSpec("opponent_team", "opponent_team", pl.Int64),
    ColumnSpec("minutes", "minutes", pl.Int64),
    ColumnSpec("starts", "starts", pl.Int64, required=False, group="expected"),
    ColumnSpec("goals_scored", "goals_scored", pl.Int64),
    ColumnSpec("assists", "assists", pl.Int64),
    ColumnSpec("goals_conceded", "goals_conceded", pl.Int64),
    ColumnSpec("own_goals", "own_goals", pl.Int64),
    ColumnSpec("penalties_saved", "penalties_saved", pl.Int64),
    ColumnSpec("penalties_missed", "penalties_missed", pl.Int64),
    ColumnSpec("yellow_cards", "yellow_cards", pl.Int64),
    ColumnSpec("red_cards", "red_cards", pl.Int64),
    ColumnSpec("saves", "saves", pl.Int64),
    ColumnSpec("bonus_fpl", "bonus", pl.Int64),
    ColumnSpec("bps_fpl", "bps", pl.Int64),
    ColumnSpec("total_points_fpl", "total_points", pl.Int64),
    ColumnSpec("team_h_score", "team_h_score", pl.Int64, required=False),
    ColumnSpec("team_a_score", "team_a_score", pl.Int64, required=False),
    ColumnSpec("expected_goals", "expected_goals", pl.Float64, required=False, group="expected"),
    ColumnSpec(
        "expected_assists", "expected_assists", pl.Float64, required=False, group="expected"
    ),
    ColumnSpec(
        "expected_goal_involvements",
        "expected_goal_involvements",
        pl.Float64,
        required=False,
        group="expected",
    ),
    ColumnSpec(
        "expected_goals_conceded",
        "expected_goals_conceded",
        pl.Float64,
        required=False,
        group="expected",
    ),
    ColumnSpec(
        "clearances_blocks_interceptions",
        "clearances_blocks_interceptions",
        pl.Int64,
        required=False,
        group="defensive",
    ),
    ColumnSpec("tackles", "tackles", pl.Int64, required=False, group="defensive"),
    ColumnSpec("recoveries", "recoveries", pl.Int64, required=False, group="defensive"),
    ColumnSpec(
        "defensive_contribution",
        "defensive_contribution",
        pl.Int64,
        required=False,
        group="defensive",
    ),
)

MERGED_GW_SPECS: dict[str, TableSpec] = {
    "E7": TableSpec(
        table="player_fixture_stats",
        key=("player_id", "fixture_id"),
        columns=_E7_COLUMNS,
        encoding="utf-8",
        drop=frozenset(
            {
                "xP",
                "value",
                "selected",
                "transfers_in",
                "transfers_out",
                "transfers_balance",
                "creativity",
                "influence",
                "threat",
                "ict_index",
                "modified",
            }
        ),
    ),
}


@dataclass(frozen=True)
class StagedMergedGw:
    frame: pl.DataFrame
    report: StagingReport
    excluded_manager_rows: int
    duplicate_rows_dropped: int = 0
    """Exact byte-for-byte duplicate CSV rows dropped before staging.

    Verified live against the real 2025/26 archive: certain players (e.g.
    element 100, "Junior Kroupi") appear with every single field identical
    across two consecutive rows for the same fixture, throughout the whole
    file. This is a genuine upstream archive defect, not a staging bug — the
    duplicate is dropped here (a safe operation, since the two rows carry
    identical data) rather than surfacing as a key-uniqueness violation
    downstream, where it would look like our own bug."""


def era_for_season(season: Season) -> str:
    """The schema era a season's ``merged_gw.csv`` was published in.

    Raises rather than guessing, per :data:`ERA_BY_SEASON` — a season this
    map has never seen must be classified by a person before it is staged.
    """
    try:
        return ERA_BY_SEASON[season]
    except KeyError:
        raise ValueError(
            f"no schema era classified for season {season}; "
            "classify it in fpl.staging.vaastav.ERA_BY_SEASON before staging"
        ) from None


def stage_merged_gw(body: bytes, season: Season) -> StagedMergedGw:
    """Stage one season's ``merged_gw.csv`` into ``player_fixture_stats`` rows.

    Manager-asset rows (Finding 6, ``position == 'AM'``) are excluded here —
    the count is returned so a caller can assert it rather than silently
    absorb it.
    """
    era = era_for_season(season)
    spec = MERGED_GW_SPECS[era]
    encoding = _ENCODING_BY_ERA[era]

    raw = decode_csv(body, encoding)

    is_manager_row = pl.col("position") == _MANAGER_ASSET_POSITION
    excluded = raw.filter(is_manager_row).height
    raw = raw.filter(~is_manager_row)

    rows_before_dedupe = raw.height
    raw = raw.unique(maintain_order=True)
    duplicate_rows_dropped = rows_before_dedupe - raw.height

    staged, report = stage_frame(raw, spec)
    staged = staged.with_columns(pl.lit(str(season)).alias("season")).select(
        ["season", *staged.columns]
    )
    return StagedMergedGw(
        frame=staged,
        report=report,
        excluded_manager_rows=excluded,
        duplicate_rows_dropped=duplicate_rows_dropped,
    )
