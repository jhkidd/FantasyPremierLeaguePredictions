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

import logging
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet, write_parquet

logger = logging.getLogger(__name__)

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
    "team_code",
    "opponent_team_id",
    "opponent_team_code",
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


def _derive_team_id_from_fixture(stats: pl.DataFrame, season: Season) -> pl.DataFrame:
    """Set each row's ``team_id`` from the fixture's own opponent column.

    A fixture is played by exactly two teams, so the distinct
    ``opponent_team_id`` values recorded against a fixture *are* those two
    teams — and a row's own team is whichever of the pair is not that row's
    opponent. This needs no external table, which is the point: the previous
    name-join depended on a ``teams`` table that only ever existed for the
    current season, so six seasons silently resolved to all-null (plan §0.3).

    It also *corrects* rather than trusts any incoming ``team_id``. The early
    eras derived theirs from ``players_raw.csv``, an end-of-season snapshot
    that misattributes every mid-season transfer, so the recorded value is
    wrong for ~1,080 rows across 2016-17 to 2019-20.

    Raises ``ValueError`` when a fixture names three or more distinct
    opponents, which no real match can do and so means the source is corrupt.
    A fixture naming only one opponent has just a single side present; its own
    team is genuinely unknowable, so it is left null for the quality gate to
    catch rather than guessed at.
    """
    teams_per_fixture = (
        stats.select("fixture_id", "opponent_team_id")
        .drop_nulls("opponent_team_id")
        .unique()
        .group_by("fixture_id")
        .agg(pl.col("opponent_team_id").sort().alias("teams"))
    )

    overfull = teams_per_fixture.filter(pl.col("teams").list.len() > 2)
    if overfull.height:
        first = overfull.sort("fixture_id").row(0, named=True)
        raise ValueError(
            f"season {season} fixture {first['fixture_id']} names "
            f"{len(first['teams'])} distinct opponent teams ({first['teams']}); "
            "a fixture is played by exactly two teams, so this source is corrupt"
        )

    stats = stats.join(teams_per_fixture, on="fixture_id", how="left")
    return stats.with_columns(
        pl.when((pl.col("teams").list.len() == 2) & pl.col("opponent_team_id").is_not_null())
        .then(
            # The pair minus this row's opponent leaves exactly its own team.
            pl.when(pl.col("teams").list.first() == pl.col("opponent_team_id"))
            .then(pl.col("teams").list.last())
            .otherwise(pl.col("teams").list.first())
        )
        .otherwise(None)
        .cast(pl.Int64)
        .alias("team_id")
    ).drop("teams")


def _with_team_codes(
    stats: pl.DataFrame, season: Season, *, data_root: Path | None
) -> pl.DataFrame:
    """Attach the season-stable ``team_code`` for a row's own and opposing team.

    ``team_id`` is reassigned alphabetically by FPL every season — id 3 is
    Brighton in 2020/21, Bournemouth in 2022/23 and Burnley in 2025/26 — so it
    cannot key anything across seasons. ``code`` can (plan §0.4).

    Left null, with the reason logged, when the season's ``teams`` table is
    absent: a facts build must not start depending on another source having
    run first, which is precisely the coupling that produced the all-null
    ``team_id`` bug this module was just repaired for.
    """
    teams_path = paths.staged_table("teams", season, data_root=data_root) / "part.parquet"
    if not teams_path.exists():
        logger.warning(
            "season %s: no staged teams table, so team_code/opponent_team_code "
            "are null for all %d row(s); run `fpl stage vaastav` for this season",
            season,
            stats.height,
        )
        return stats.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("team_code"),
            pl.lit(None, dtype=pl.Int64).alias("opponent_team_code"),
        )

    teams = read_parquet(teams_path).select(
        pl.col("team_id").cast(pl.Int64), pl.col("code").cast(pl.Int64)
    )
    stats = stats.join(
        teams.rename({"code": "team_code"}), on="team_id", how="left"
    )
    stats = stats.join(
        teams.rename({"team_id": "opponent_team_id", "code": "opponent_team_code"}),
        on="opponent_team_id",
        how="left",
    )
    return stats


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

    rename_map = {
        column: target
        for column, target in {
            "opponent_team": "opponent_team_id",
            "clearances_blocks_interceptions": "cbi",
        }.items()
        if column in stats.columns
    }
    if rename_map:
        stats = stats.rename(rename_map)

    # Unconditional, and deliberately ignores any incoming ``team_id``: every
    # era that ships one ships a wrong one (plan §0.3).
    stats = _derive_team_id_from_fixture(stats, season)
    stats = _with_team_codes(stats, season, data_root=data_root)

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
