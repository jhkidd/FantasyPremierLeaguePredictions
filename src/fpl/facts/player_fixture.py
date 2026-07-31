"""Facts assembly: staged player-fixture stats -> canonical ``player_fixture``
facts (spec §5, plan §5.4).

Primary key is ``(season, fixture_id, player_id)``, enforced. A fixture with
no recorded performance yields zero rows — never a null row (spec §6).

Column groups mirror the plan: keys, match context, core components,
defensive contribution inputs, BPS inputs, expected-stats, FPL's own observed
output (suffixed ``_fpl`` so a feature-builder reaching for ``total_points``
gets a ``KeyError``, not a leak), and a four-boolean availability mask.

The mask is per *group*, not per column (spec §4 asks for per-column; in
practice only four independent presence patterns occur across all seven
schema eras, so four booleans carry identical information at a tenth the
width — the plan's locked decision). It is stored per row, not per season, so
a consumer never has to consult the era map itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet, write_parquet

__all__ = [
    "KEY",
    "FactsResult",
    "build_player_fixture_facts",
    "write_player_fixture_facts",
]

KEY: tuple[str, ...] = ("season", "fixture_id", "player_id")

# BPS-input columns (Finding 2) are observed for 2016/17-2018/19 only — absent
# in every other era including 2025/26 (E7). Declared here and filled with
# null so the schema never changes when phase 6 adds the eras that carry them.
_BPS_INPUT_COLUMNS: tuple[str, ...] = (
    "attempted_passes",
    "completed_passes",
    "key_passes",
    "big_chances_created",
    "big_chances_missed",
    "open_play_crosses",
    "dribbles",
    "tackled",
    "fouls",
    "offside",
    "target_missed",
    "errors_leading_to_goal",
    "errors_leading_to_goal_attempt",
    "penalties_conceded",
    "winning_goals",
)

_DEFENSIVE_COLUMNS: tuple[str, ...] = ("cbi", "tackles", "recoveries", "defensive_contribution")

_EXPECTED_COLUMNS: tuple[str, ...] = (
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
)

_CORE_COLUMNS: tuple[str, ...] = (
    "goals_scored",
    "assists",
    "goals_conceded",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "saves",
)

_OBSERVED_FPL_COLUMNS: tuple[str, ...] = ("total_points_fpl", "bonus_fpl", "bps_fpl")

_COLUMN_ORDER: tuple[str, ...] = (
    "season",
    "fixture_id",
    "player_id",
    "player_code",
    "team_id",
    "opponent_team_id",
    "was_home",
    "kickoff_time",
    "event",
    "position",
    "minutes",
    "starts",
    *_CORE_COLUMNS,
    *_DEFENSIVE_COLUMNS,
    *_BPS_INPUT_COLUMNS,
    *_EXPECTED_COLUMNS,
    *_OBSERVED_FPL_COLUMNS,
    "obs_defensive",
    "obs_bps_inputs",
    "obs_expected",
    "obs_starts",
)


@dataclass(frozen=True)
class FactsResult:
    frame: pl.DataFrame | None
    written: bool
    detail: str = ""


def _team_id_lookup(season: Season, *, data_root: Path | None = None) -> pl.DataFrame | None:
    """Own-team name -> FPL team id, from this season's staged ``teams`` table.

    ``merged_gw.csv`` carries the player's own team as a name string and the
    opponent as a numeric id (an archive quirk, not a bug) — this join
    resolves the former into the same id space as the latter. Returns
    ``None`` (never raises) when the ``teams`` table has not been staged for
    this season, so a facts build never depends on an unrelated source having
    already run."""
    teams_path = paths.staged_table("teams", season, data_root=data_root) / "part.parquet"
    if not teams_path.exists():
        return None
    return read_parquet(teams_path).select(["team_id", "name"])


def _with_null_column(frame: pl.DataFrame, name: str, dtype: pl.DataType) -> pl.DataFrame:
    if name in frame.columns:
        return frame
    return frame.with_columns(pl.lit(None, dtype=dtype).alias(name))


def build_player_fixture_facts(
    season: Season, *, data_root: Path | None = None
) -> pl.DataFrame | None:
    """Assemble one season's ``player_fixture`` facts from staged tables.

    Returns ``None`` when ``player_fixture_stats`` has not been staged for
    this season yet — there is nothing to assemble, and that is a normal,
    expected state rather than an error.
    """
    stats_path = paths.staged_table("player_fixture_stats", season, data_root=data_root)
    stats_path = stats_path / "part.parquet"
    if not stats_path.exists():
        return None
    stats = read_parquet(stats_path)

    team_lookup = _team_id_lookup(season, data_root=data_root)
    if team_lookup is not None:
        stats = stats.join(team_lookup, left_on="team", right_on="name", how="left")
    else:
        stats = stats.with_columns(pl.lit(None, dtype=pl.Int64).alias("team_id"))

    stats = stats.rename(
        {
            "opponent_team": "opponent_team_id",
            "clearances_blocks_interceptions": "cbi",
        }
    )
    stats = stats.with_columns(
        pl.col("kickoff_time").str.strptime(
            pl.Datetime(time_unit="us", time_zone="UTC"), strict=False
        )
    )
    stats = _with_null_column(stats, "player_code", pl.Utf8)
    stats = _with_null_column(stats, "starts", pl.Int64)
    for column in _DEFENSIVE_COLUMNS:
        stats = _with_null_column(stats, column, pl.Int64)
    for column in _EXPECTED_COLUMNS:
        stats = _with_null_column(stats, column, pl.Float64)
    for column in _BPS_INPUT_COLUMNS:
        stats = _with_null_column(stats, column, pl.Int64)

    stats = stats.with_columns(
        pl.any_horizontal([pl.col(c).is_not_null() for c in _DEFENSIVE_COLUMNS]).alias(
            "obs_defensive"
        ),
        pl.any_horizontal([pl.col(c).is_not_null() for c in _BPS_INPUT_COLUMNS]).alias(
            "obs_bps_inputs"
        ),
        pl.any_horizontal([pl.col(c).is_not_null() for c in _EXPECTED_COLUMNS]).alias(
            "obs_expected"
        ),
        pl.col("starts").is_not_null().alias("obs_starts"),
    )

    dupes = stats.select(list(KEY)).is_duplicated().sum()
    if dupes:
        raise ValueError(
            f"player_fixture key {KEY} is not unique in season {season}: {dupes} duplicate row(s)"
        )

    return stats.select(list(_COLUMN_ORDER))


def write_player_fixture_facts(season: Season, *, data_root: Path | None = None) -> FactsResult:
    """Build and write ``facts/player_fixture/season=.../part.parquet``.

    Idempotent and deterministic — an unchanged rebuild produces an empty
    Git diff (spec §11)."""
    frame = build_player_fixture_facts(season, data_root=data_root)
    if frame is None:
        return FactsResult(None, False, "no player_fixture_stats staged for this season")

    out_dir = paths.facts_table("player_fixture", season, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet", sort_by=list(KEY))
    return FactsResult(frame, True)
