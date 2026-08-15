"""Tests for :mod:`fpl.training.era_continuity`'s defensive-contribution
era-continuity experiment (Phase A Step 32, plan Q8/A8)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

from fpl.config import Season
from fpl.scoring.base import POSITIONS
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet
from fpl.training.era_continuity import (
    DC_ERA_TRAIN_SEASONS,
    _derived_defensive_contribution_labels,
    defensive_contribution_era_continuity_report,
)

ERA_SEASON = DC_ERA_TRAIN_SEASONS[0]
TEST_SEASON = "2025-26"


def _write_era_facts(data_root: Path, season: str, rows: list[dict]) -> None:
    frame = pl.DataFrame(rows)
    out_dir = paths.facts_table("player_fixture", Season.parse(season), data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet")


class TestDerivedDefensiveContributionLabels:
    def test_defenders_use_cbi_plus_tackles(self, tmp_path: Path) -> None:
        _write_era_facts(
            tmp_path,
            ERA_SEASON,
            [
                {
                    "season": ERA_SEASON,
                    "fixture_id": 1,
                    "player_id": 1,
                    "position": "DEF",
                    "cbi": 5,
                    "tackles": 3,
                    "recoveries": 7,
                }
            ],
        )

        result = _derived_defensive_contribution_labels(DC_ERA_TRAIN_SEASONS, data_root=tmp_path)

        assert result["label_defensive_contribution"].to_list() == [8.0]

    def test_midfielders_and_forwards_add_recoveries_too(self, tmp_path: Path) -> None:
        _write_era_facts(
            tmp_path,
            ERA_SEASON,
            [
                {
                    "season": ERA_SEASON,
                    "fixture_id": 1,
                    "player_id": 1,
                    "position": "MID",
                    "cbi": 2,
                    "tackles": 3,
                    "recoveries": 4,
                },
                {
                    "season": ERA_SEASON,
                    "fixture_id": 2,
                    "player_id": 2,
                    "position": "FWD",
                    "cbi": 1,
                    "tackles": 1,
                    "recoveries": 1,
                },
            ],
        )

        result = _derived_defensive_contribution_labels(
            DC_ERA_TRAIN_SEASONS, data_root=tmp_path
        ).sort("player_id")

        assert result["label_defensive_contribution"].to_list() == [9.0, 3.0]

    def test_goalkeepers_are_excluded(self, tmp_path: Path) -> None:
        _write_era_facts(
            tmp_path,
            ERA_SEASON,
            [
                {
                    "season": ERA_SEASON,
                    "fixture_id": 1,
                    "player_id": 1,
                    "position": "GK",
                    "cbi": 5,
                    "tackles": 3,
                    "recoveries": 7,
                }
            ],
        )

        result = _derived_defensive_contribution_labels(DC_ERA_TRAIN_SEASONS, data_root=tmp_path)

        assert result.height == 0

    def test_rows_missing_any_raw_component_are_excluded(self, tmp_path: Path) -> None:
        _write_era_facts(
            tmp_path,
            ERA_SEASON,
            [
                {
                    "season": ERA_SEASON,
                    "fixture_id": 1,
                    "player_id": 1,
                    "position": "DEF",
                    "cbi": None,
                    "tackles": 3,
                    "recoveries": 7,
                }
            ],
        )

        result = _derived_defensive_contribution_labels(DC_ERA_TRAIN_SEASONS, data_root=tmp_path)

        assert result.height == 0

    def test_a_season_with_no_built_facts_is_silently_skipped(self, tmp_path: Path) -> None:
        result = _derived_defensive_contribution_labels(DC_ERA_TRAIN_SEASONS, data_root=tmp_path)

        assert result.height == 0
        assert result.columns == [
            "season",
            "fixture_id",
            "player_id",
            "label_defensive_contribution",
        ]


def _matrix_frame(*, seed: int = 0, n_per_position: int = 30) -> pl.DataFrame:
    """A synthetic training-matrix-shaped frame spanning one era-training
    season (``ERA_SEASON``, with ``label_defensive_contribution`` left
    null exactly as real data is) and the test season (``TEST_SEASON``,
    with a real, non-null label) - big enough per position for
    Ridge/Poisson to fit without a convergence warning."""
    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    fixture_id = 0
    player_id = 0
    for season in (ERA_SEASON, TEST_SEASON):
        for position in sorted(POSITIONS):
            for event in range(1, n_per_position + 1):
                fixture_id += 1
                player_id += 1
                feature_a = rng.normal()
                feature_b = rng.normal()
                played = rng.random() > 0.2
                minutes = float(rng.integers(60, 90)) if played else 0.0
                dc_value = float(rng.poisson(5.0)) if played else 0.0
                rows.append(
                    {
                        "season": season,
                        "event": event,
                        "fixture_id": fixture_id,
                        "player_id": player_id,
                        "position": position,
                        "feature_a": feature_a,
                        "feature_b": feature_b,
                        "cbi_sum_last_3": feature_a,
                        "obs_defensive": True,
                        "obs_bps_inputs": True,
                        "obs_expected": True,
                        "obs_starts": True,
                        "label_minutes": minutes,
                        "label_defensive_contribution": (
                            dc_value if season == TEST_SEASON else None
                        ),
                    }
                )
    return pl.DataFrame(
        rows,
        schema_overrides={"label_defensive_contribution": pl.Float64, "label_minutes": pl.Float64},
    )


class TestDefensiveContributionEraContinuityReport:
    def test_reports_overall_and_outfield_position_groups_for_both_models(
        self, tmp_path: Path
    ) -> None:
        full_frame = _matrix_frame()
        rows_by_position = {
            position: [
                {
                    "season": ERA_SEASON,
                    "fixture_id": int(row["fixture_id"]),
                    "player_id": int(row["player_id"]),
                    "position": position,
                    "cbi": 2,
                    "tackles": 1,
                    "recoveries": 3,
                }
                for row in full_frame.filter(
                    (pl.col("season") == ERA_SEASON) & (pl.col("position") == position)
                ).iter_rows(named=True)
            ]
            for position in sorted(POSITIONS)
        }
        era_facts_rows = [row for rows in rows_by_position.values() for row in rows]
        _write_era_facts(tmp_path, ERA_SEASON, era_facts_rows)

        report = defensive_contribution_era_continuity_report(full_frame, data_root=tmp_path)

        assert set(report["model"].unique().to_list()) == {"glm", "naive"}
        groups = set(report["group"].unique().to_list())
        assert groups == {"overall", "DEF", "MID", "FWD"}
        assert "GK" not in groups

    def test_overall_glm_row_has_predictions_evaluated_against_real_test_labels(
        self, tmp_path: Path
    ) -> None:
        full_frame = _matrix_frame()
        era_rows = [
            {
                "season": ERA_SEASON,
                "fixture_id": int(row["fixture_id"]),
                "player_id": int(row["player_id"]),
                "position": row["position"],
                "cbi": 2,
                "tackles": 1,
                "recoveries": 3,
            }
            for row in full_frame.filter(
                (pl.col("season") == ERA_SEASON) & (pl.col("position") != "GK")
            ).iter_rows(named=True)
        ]
        _write_era_facts(tmp_path, ERA_SEASON, era_rows)

        report = defensive_contribution_era_continuity_report(full_frame, data_root=tmp_path)

        overall_glm = report.filter((pl.col("group") == "overall") & (pl.col("model") == "glm"))
        assert overall_glm["n"].to_list()[0] > 0
