"""Tests for :mod:`fpl.inference.predict` — the production inference
entrypoint (Phase C step 4, `.github/context/subsystem3-close-and-mvp-site.md`)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from fpl.config import Season
from fpl.inference.predict import predict_next_gameweek
from fpl.scoring.base import POSITIONS
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet
from fpl.training.baseline import GLM_COMPONENTS, MINUTES_TARGET, fit_glm_baseline
from fpl.training.registry import COMPONENT_NAMES, save_component_artefact, write_active_pointer

SEASON = Season(2025)

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


def _save_bundle(models_root: Path, feature_column: str, *, version: str = "v1") -> None:
    """A GLM bundle whose sole feature is ``feature_column`` — pass a real
    rolling-feature name ``features.library.build`` always populates (e.g.
    ``goals_scored_sum_last_3``) for a bundle the inference frame satisfies,
    or an unrelated name to exercise the missing-feature hard fail."""
    rows = [
        {
            "position": position,
            "obs_defensive": True,
            "obs_bps_inputs": True,
            "obs_expected": True,
            "obs_starts": True,
            feature_column: float(i % 3),
            "label_minutes": 90.0,
            "label_goals_scored": float(i % 2),
            "label_assists": float(i % 2),
            "label_goals_conceded": 1.0,
            "label_bonus": float(i % 3),
            "label_defensive_contribution": float(i % 4),
        }
        for position in sorted(POSITIONS)
        for i in range(10)
    ]
    train_frame = pl.DataFrame(rows)
    bundle = fit_glm_baseline(train_frame)

    write_active_pointer(
        {component: {"model": "glm", "version": version} for component in COMPONENT_NAMES},
        models_root=models_root,
    )
    save_component_artefact(
        MINUTES_TARGET,
        "glm",
        version,
        bundle.minutes_models,
        feature_columns=bundle.feature_columns,
        models_root=models_root,
    )
    for component in GLM_COMPONENTS:
        models_for_component = {
            position: pipeline
            for (comp, position), pipeline in bundle.component_models.items()
            if comp == component
        }
        save_component_artefact(
            component,
            "glm",
            version,
            models_for_component,
            feature_columns=bundle.feature_columns,
            models_root=models_root,
        )


class TestPredictNextGameweek:
    def test_no_active_model_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="active.json"):
            predict_next_gameweek(
                SEASON,
                datetime(2025, 8, 20, tzinfo=UTC),
                data_root=tmp_path / "data",
                models_root=tmp_path / "models",
            )

    def test_missing_staged_tables_returns_empty_frame(self, tmp_path: Path) -> None:
        models_root = tmp_path / "models"
        _save_bundle(models_root, "goals_scored_sum_last_3")

        result = predict_next_gameweek(
            SEASON,
            datetime(2025, 8, 20, tzinfo=UTC),
            data_root=tmp_path / "data",
            models_root=models_root,
        )

        assert result.height == 0

    def test_missing_required_feature_column_raises(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        models_root = tmp_path / "models"
        _save_bundle(models_root, "not_a_real_feature")
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

        with pytest.raises(ValueError, match="not_a_real_feature"):
            predict_next_gameweek(
                SEASON,
                datetime(2025, 8, 20, tzinfo=UTC),
                data_root=data_root,
                models_root=models_root,
            )

    def test_predicts_total_points_for_horizon_row(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        models_root = tmp_path / "models"
        _save_bundle(models_root, "goals_scored_sum_last_3")

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
        # History before as_of, so every naive-only component has real
        # values rather than assemble_predicted_points nulling the row out.
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
                    goals_scored=1,
                    saves=0,
                    yellow_cards=0,
                    red_cards=0,
                    penalties_saved=0,
                    penalties_missed=0,
                    own_goals=0,
                )
            ],
        )

        result = predict_next_gameweek(
            SEASON, datetime(2025, 8, 20, tzinfo=UTC), data_root=data_root, models_root=models_root
        )

        assert result.height == 1
        assert result["fixture_id"].to_list() == [501]
        assert "predicted_total_points_fpl" in result.columns
        assert result["predicted_total_points_fpl"].to_list()[0] is not None

    def test_double_gameweek_both_rows_get_same_naive_prediction(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        models_root = tmp_path / "models"
        _save_bundle(models_root, "goals_scored_sum_last_3")

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
                    saves=2,
                )
            ],
        )

        result = predict_next_gameweek(
            SEASON, datetime(2025, 8, 20, tzinfo=UTC), data_root=data_root, models_root=models_root
        )

        assert result.height == 2
        assert result["naive_saves"].to_list() == [2.0, 2.0]
