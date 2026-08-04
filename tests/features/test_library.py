from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.features.library import build
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet

SEASON = Season(2025)
PREV_SEASON = Season(2024)


def _write_players(data_root: Path, season: Season, rows: list[dict]) -> None:
    frame = pl.DataFrame(rows)
    out_dir = paths.staged_table("players", season, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet")


def _write_fixtures(data_root: Path, season: Season, rows: list[dict]) -> None:
    frame = pl.DataFrame(rows)
    out_dir = paths.staged_table("fixtures", season, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet")


_FACTS_COLUMNS = [
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
            "player_code": None,
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


class TestBuild:
    def test_missing_staged_tables_returns_none_frame(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        result = build(SEASON, datetime(2025, 8, 15, tzinfo=UTC), data_root=data_root)

        assert result.frame is None
        assert result.detail

    def test_one_row_per_player_fixture_in_default_horizon(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root, SEASON, [{"player_id": 1, "team_id": 3, "element_type": 3, "now_cost": 75}]
        )
        _write_fixtures(
            data_root,
            SEASON,
            [
                {
                    "fixture_id": 501,
                    "event": 2,
                    "kickoff_time": "2025-08-23T14:00:00Z",
                    "team_h": 3,
                    "team_a": 7,
                    "finished": False,
                },
                {
                    "fixture_id": 600,
                    "event": 3,
                    "kickoff_time": "2025-08-30T14:00:00Z",
                    "team_h": 3,
                    "team_a": 9,
                    "finished": False,
                },
            ],
        )

        result = build(SEASON, datetime(2025, 8, 20, tzinfo=UTC), data_root=data_root)

        assert result.frame is not None
        assert result.frame.height == 1
        assert result.frame["fixture_id"].to_list() == [501]

    def test_double_gameweek_produces_two_rows(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root, SEASON, [{"player_id": 1, "team_id": 3, "element_type": 3, "now_cost": 75}]
        )
        _write_fixtures(
            data_root,
            SEASON,
            [
                {
                    "fixture_id": 501,
                    "event": 2,
                    "kickoff_time": "2025-08-23T14:00:00Z",
                    "team_h": 3,
                    "team_a": 7,
                    "finished": False,
                },
                {
                    "fixture_id": 502,
                    "event": 2,
                    "kickoff_time": "2025-08-26T19:00:00Z",
                    "team_h": 9,
                    "team_a": 3,
                    "finished": False,
                },
            ],
        )

        result = build(SEASON, datetime(2025, 8, 20, tzinfo=UTC), data_root=data_root)

        assert result.frame.height == 2
        assert sorted(result.frame["fixture_id"].to_list()) == [501, 502]

    def test_blank_gameweek_team_yields_no_rows(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root, SEASON, [{"player_id": 1, "team_id": 3, "element_type": 3, "now_cost": 75}]
        )
        _write_fixtures(
            data_root,
            SEASON,
            [
                {
                    "fixture_id": 700,
                    "event": 2,
                    "kickoff_time": "2025-08-23T14:00:00Z",
                    "team_h": 5,
                    "team_a": 7,
                    "finished": False,
                }
            ],
        )

        result = build(SEASON, datetime(2025, 8, 20, tzinfo=UTC), data_root=data_root)

        assert result.frame.height == 0

    def test_position_and_price_resolved(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root, SEASON, [{"player_id": 1, "team_id": 3, "element_type": 4, "now_cost": 80}]
        )
        _write_fixtures(
            data_root,
            SEASON,
            [
                {
                    "fixture_id": 501,
                    "event": 2,
                    "kickoff_time": "2025-08-23T14:00:00Z",
                    "team_h": 3,
                    "team_a": 7,
                    "finished": False,
                }
            ],
        )

        result = build(SEASON, datetime(2025, 8, 20, tzinfo=UTC), data_root=data_root)

        row = result.frame.row(0, named=True)
        assert row["position"] == "FWD"
        assert row["price"] == 80

    def test_label_populated_when_fixture_already_played(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root, SEASON, [{"player_id": 1, "team_id": 3, "element_type": 3, "now_cost": 75}]
        )
        _write_fixtures(
            data_root,
            SEASON,
            [
                {
                    "fixture_id": 501,
                    "event": 1,
                    "kickoff_time": "2025-08-16T14:00:00Z",
                    "team_h": 3,
                    "team_a": 7,
                    "finished": True,
                }
            ],
        )
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=501,
                    player_id=1,
                    team_id=3,
                    event=1,
                    kickoff_time=datetime(2025, 8, 16, 14, tzinfo=UTC),
                    minutes=90,
                    total_points_fpl=6,
                )
            ],
        )

        result = build(SEASON, datetime(2025, 8, 10, tzinfo=UTC), data_root=data_root)

        row = result.frame.row(0, named=True)
        assert row["label_minutes"] == 90
        assert row["label_total_points_fpl"] == 6

    def test_label_null_when_fixture_not_yet_played(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root, SEASON, [{"player_id": 1, "team_id": 3, "element_type": 3, "now_cost": 75}]
        )
        _write_fixtures(
            data_root,
            SEASON,
            [
                {
                    "fixture_id": 501,
                    "event": 2,
                    "kickoff_time": "2025-08-23T14:00:00Z",
                    "team_h": 3,
                    "team_a": 7,
                    "finished": False,
                }
            ],
        )

        result = build(SEASON, datetime(2025, 8, 20, tzinfo=UTC), data_root=data_root)

        row = result.frame.row(0, named=True)
        assert row["label_minutes"] is None
        assert row["label_total_points_fpl"] is None

    def test_rolling_features_present_from_prior_history(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root, SEASON, [{"player_id": 1, "team_id": 3, "element_type": 3, "now_cost": 75}]
        )
        _write_fixtures(
            data_root,
            SEASON,
            [
                {
                    "fixture_id": 501,
                    "event": 2,
                    "kickoff_time": "2025-08-23T14:00:00Z",
                    "team_h": 3,
                    "team_a": 7,
                    "finished": False,
                }
            ],
        )
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=400,
                    player_id=1,
                    team_id=3,
                    event=1,
                    kickoff_time=datetime(2025, 8, 16, 14, tzinfo=UTC),
                    minutes=90,
                    goals_scored=2,
                )
            ],
        )

        result = build(SEASON, datetime(2025, 8, 20, tzinfo=UTC), data_root=data_root)

        row = result.frame.row(0, named=True)
        assert row["goals_scored_sum_last_3"] == 2
        assert row["goals_scored_sum_last_season_to_date"] == 2

    def test_team_context_columns_joined(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root, SEASON, [{"player_id": 1, "team_id": 3, "element_type": 3, "now_cost": 75}]
        )
        _write_fixtures(
            data_root,
            SEASON,
            [
                {
                    "fixture_id": 501,
                    "event": 2,
                    "kickoff_time": "2025-08-23T14:00:00Z",
                    "team_h": 3,
                    "team_a": 7,
                    "finished": False,
                }
            ],
        )
        team_fixture_frame = pl.DataFrame(
            [
                {
                    "season": str(SEASON),
                    "fixture_id": 501,
                    "team_id": 3,
                    "opponent_team_id": 7,
                    "was_home": True,
                    "elo_rating": 1600.0,
                    "opponent_elo_rating": 1500.0,
                    "fixture_count_prior_7_days": 1,
                    "fixture_count_prior_14_days": 2,
                    "fixture_count_prior_28_days": 4,
                    "odds_implied_win_prob": 0.6,
                    "odds_implied_draw_prob": 0.25,
                    "odds_implied_loss_prob": 0.15,
                }
            ]
        )
        out_dir = paths.facts_table("team_fixture", SEASON, data_root=data_root)
        out_dir.mkdir(parents=True, exist_ok=True)
        write_parquet(team_fixture_frame, out_dir / "part.parquet")

        result = build(SEASON, datetime(2025, 8, 20, tzinfo=UTC), data_root=data_root)

        row = result.frame.row(0, named=True)
        assert row["elo_rating"] == 1600.0
        assert row["odds_implied_win_prob"] == 0.6

    def test_diagnostics_returned_for_fallback_players(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root, SEASON, [{"player_id": 1, "team_id": 3, "element_type": 3, "now_cost": 75}]
        )
        _write_fixtures(
            data_root,
            SEASON,
            [
                {
                    "fixture_id": 501,
                    "event": 2,
                    "kickoff_time": "2025-08-23T14:00:00Z",
                    "team_h": 3,
                    "team_a": 7,
                    "finished": False,
                }
            ],
        )

        result = build(SEASON, datetime(2025, 8, 20, tzinfo=UTC), data_root=data_root)

        assert result.diagnostics.fallback_to_current_team == (1,)
