from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from fpl.config import Season
from fpl.features.team_context import TEAM_CONTEXT_COLUMNS
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet
from fpl.training.dataset import IDENTITY_COLUMNS, LABEL_COLUMNS, build_training_matrix

SEASON = Season(2025)
PREV_SEASON = Season(2024)


def _kickoff(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# Mirrors fpl.facts.player_fixture's _COLUMN_ORDER exactly (including the
# team_code/opponent_team_code columns that tests/features/test_library.py's
# older helper predates).
_FACTS_COLUMNS = [
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
    "goals_scored",
    "assists",
    "goals_conceded",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "saves",
    "cbi",
    "tackles",
    "recoveries",
    "defensive_contribution",
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
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
    "total_points_fpl",
    "bonus_fpl",
    "bps_fpl",
    "obs_defensive",
    "obs_bps_inputs",
    "obs_expected",
    "obs_starts",
]


def _facts_row(**overrides: object) -> dict:
    row: dict = dict.fromkeys(_FACTS_COLUMNS, 0)
    row.update(
        {
            "season": str(SEASON),
            "player_code": "code-1",
            "team_code": 1,
            "opponent_team_code": 2,
            "was_home": True,
            "position": "MID",
            "obs_defensive": True,
            "obs_bps_inputs": True,
            "obs_expected": True,
            "obs_starts": True,
        }
    )
    row.update(overrides)
    return row


def _write_facts(data_root: Path, season: Season, rows: list[dict]) -> None:
    frame = pl.DataFrame(rows)
    out_dir = paths.facts_table("player_fixture", season, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet")


def _write_team_fixture(data_root: Path, season: Season, rows: list[dict]) -> None:
    frame = pl.DataFrame(
        rows,
        schema={
            "fixture_id": pl.Int64,
            "team_id": pl.Int64,
            **{column: pl.Float64 for column in TEAM_CONTEXT_COLUMNS},
        },
    )
    out_dir = paths.facts_table("team_fixture", season, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet")


class TestBuildTrainingMatrix:
    def test_row_count_matches_facts_row_count(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=1,
                    player_id=1,
                    event=1,
                    kickoff_time=_kickoff("2025-08-16T14:00:00"),
                    minutes=90,
                ),
                _facts_row(
                    fixture_id=2,
                    player_id=1,
                    event=2,
                    kickoff_time=_kickoff("2025-08-23T14:00:00"),
                    minutes=45,
                ),
            ],
        )

        matrix = build_training_matrix([SEASON], data_root=data_root)

        assert matrix.height == 2
        for column in IDENTITY_COLUMNS:
            assert column in matrix.columns
        for column in LABEL_COLUMNS:
            assert column in matrix.columns

    def test_first_ever_row_has_null_rolling_features(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=1,
                    player_id=1,
                    event=1,
                    kickoff_time=_kickoff("2025-08-16T14:00:00"),
                    minutes=90,
                ),
            ],
        )

        matrix = build_training_matrix([SEASON], data_root=data_root)

        row = matrix.row(0, named=True)
        assert row["minutes_sum_last_3"] is None
        assert row["minutes_per90_last_3"] is None

    def test_no_leakage_from_current_or_future_gameweeks(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=1,
                    player_id=1,
                    event=1,
                    kickoff_time=_kickoff("2025-08-16T14:00:00"),
                    minutes=90,
                ),
                _facts_row(
                    fixture_id=2,
                    player_id=1,
                    event=2,
                    kickoff_time=_kickoff("2025-08-23T14:00:00"),
                    minutes=45,
                ),
                _facts_row(
                    fixture_id=3,
                    player_id=1,
                    event=3,
                    kickoff_time=_kickoff("2025-08-30T14:00:00"),
                    minutes=60,
                ),
            ],
        )

        matrix = build_training_matrix([SEASON], data_root=data_root)

        event_2_row = matrix.filter(pl.col("event") == 2).row(0, named=True)
        # Only gameweek 1's 90 minutes may be counted at gameweek 2 - gameweek
        # 2's own 45 minutes and gameweek 3's 60 minutes must never leak in.
        assert event_2_row["minutes_sum_last_3"] == 90

        event_3_row = matrix.filter(pl.col("event") == 3).row(0, named=True)
        assert event_3_row["minutes_sum_last_3"] == 90 + 45

    def test_labels_populated_regardless_of_as_of(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=1,
                    player_id=1,
                    event=1,
                    kickoff_time=_kickoff("2025-08-16T14:00:00"),
                    minutes=90,
                    goals_scored=2,
                    bonus_fpl=3,
                    total_points_fpl=15,
                ),
            ],
        )

        matrix = build_training_matrix([SEASON], data_root=data_root)

        row = matrix.row(0, named=True)
        assert row["label_minutes"] == 90
        assert row["label_goals_scored"] == 2
        assert row["label_bonus"] == 3
        assert row["label_total_points_fpl"] == 15

    def test_double_gameweek_rows_share_history_not_each_other(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=1,
                    player_id=1,
                    event=1,
                    kickoff_time=_kickoff("2025-08-16T14:00:00"),
                    minutes=90,
                ),
                _facts_row(
                    fixture_id=2,
                    player_id=1,
                    event=2,
                    kickoff_time=_kickoff("2025-08-23T14:00:00"),
                    minutes=30,
                ),
                _facts_row(
                    fixture_id=3,
                    player_id=1,
                    event=2,
                    kickoff_time=_kickoff("2025-08-24T14:00:00"),
                    minutes=60,
                ),
            ],
        )

        matrix = build_training_matrix([SEASON], data_root=data_root)

        event_2_rows = matrix.filter(pl.col("event") == 2)
        assert event_2_rows.height == 2
        # Both of gameweek 2's rows see only gameweek 1's history - neither
        # of gameweek 2's own two fixtures may leak into the other's history.
        assert event_2_rows["minutes_sum_last_3"].to_list() == [90, 90]

    def test_team_context_columns_joined(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=1,
                    player_id=1,
                    event=1,
                    team_id=10,
                    kickoff_time=_kickoff("2025-08-16T14:00:00"),
                    minutes=90,
                ),
            ],
        )
        _write_team_fixture(
            data_root,
            SEASON,
            [
                {
                    "fixture_id": 1,
                    "team_id": 10,
                    "elo_rating": 1500.0,
                    "opponent_elo_rating": 1400.0,
                    "fixture_count_prior_7_days": 1.0,
                    "fixture_count_prior_14_days": 2.0,
                    "fixture_count_prior_28_days": 3.0,
                    "odds_implied_win_prob": 0.5,
                    "odds_implied_draw_prob": 0.3,
                    "odds_implied_loss_prob": 0.2,
                }
            ],
        )

        matrix = build_training_matrix([SEASON], data_root=data_root)

        row = matrix.row(0, named=True)
        assert row["elo_rating"] == 1500.0
        assert row["opponent_elo_rating"] == 1400.0

    def test_missing_facts_raises_file_not_found(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"

        with pytest.raises(FileNotFoundError):
            build_training_matrix([SEASON], data_root=data_root)

    def test_multiple_seasons_are_concatenated(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            PREV_SEASON,
            [
                _facts_row(
                    season=str(PREV_SEASON),
                    fixture_id=101,
                    player_id=1,
                    event=1,
                    kickoff_time=_kickoff("2024-08-16T14:00:00"),
                    minutes=90,
                ),
            ],
        )
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=1,
                    player_id=1,
                    event=1,
                    kickoff_time=_kickoff("2025-08-16T14:00:00"),
                    minutes=45,
                ),
            ],
        )

        matrix = build_training_matrix([PREV_SEASON, SEASON], data_root=data_root)

        assert matrix.height == 2
        assert sorted(matrix["season"].to_list()) == [str(PREV_SEASON), str(SEASON)]

    def test_last_season_history_used_when_prior_season_facts_exist(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            PREV_SEASON,
            [
                _facts_row(
                    season=str(PREV_SEASON),
                    fixture_id=101,
                    player_id=1,
                    event=38,
                    kickoff_time=_kickoff("2025-05-18T14:00:00"),
                    minutes=90,
                ),
            ],
        )
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=1,
                    player_id=1,
                    event=1,
                    kickoff_time=_kickoff("2025-08-16T14:00:00"),
                    minutes=45,
                ),
            ],
        )

        matrix = build_training_matrix([SEASON], data_root=data_root)

        row = matrix.row(0, named=True)
        assert row["minutes_sum_last_last_season"] == 90
