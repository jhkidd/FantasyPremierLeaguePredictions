"""Tests for :mod:`fpl.training.evaluation` (Phase A Step 30)."""

from __future__ import annotations

import polars as pl
import pytest

from fpl.facts import ruleset_for_name
from fpl.scoring.base import PlayerFixtureRow
from fpl.training.evaluation import (
    OUTCOME_BUCKETS,
    assemble_predicted_points,
    component_regression_metrics,
    outcome_bucket,
    points_error_report,
    ruleset_name_for_season,
    spearman_by_gameweek,
)


class TestRulesetNameForSeason:
    def test_pre_2025_seasons_use_legacy(self) -> None:
        assert ruleset_name_for_season("2016-17") == "legacy"
        assert ruleset_name_for_season("2024-25") == "legacy"

    def test_2025_26_uses_its_own_ruleset(self) -> None:
        assert ruleset_name_for_season("2025-26") == "2025-26"

    def test_2026_27_onward_uses_its_own_ruleset(self) -> None:
        assert ruleset_name_for_season("2026-27") == "2026-27"
        assert ruleset_name_for_season("2030-31") == "2026-27"


class TestOutcomeBucket:
    def test_boundaries_match_plan_q10(self) -> None:
        assert outcome_bucket(0) == "zeros"
        assert outcome_bucket(1) == "blanks"
        assert outcome_bucket(3) == "blanks"
        assert outcome_bucket(4) == "tickers"
        assert outcome_bucket(8) == "tickers"
        assert outcome_bucket(9) == "haulers"
        assert outcome_bucket(20) == "haulers"


class TestComponentRegressionMetrics:
    def test_mae_and_rmse_on_known_errors(self) -> None:
        frame = pl.DataFrame({"actual": [1.0, 2.0, 3.0], "predicted": [2.0, 2.0, 5.0]})

        metrics = component_regression_metrics(
            frame, actual_column="actual", predicted_column="predicted"
        )

        # Errors are 1, 0, 2 -> MAE = 1.0, RMSE = sqrt((1+0+4)/3).
        assert metrics["mae"] == pytest.approx(1.0)
        assert metrics["rmse"] == pytest.approx((5.0 / 3) ** 0.5)
        assert metrics["n"] == 3
        assert metrics["poisson_deviance"] is None

    def test_null_rows_are_excluded_not_penalised(self) -> None:
        frame = pl.DataFrame({"actual": [1.0, None, 3.0], "predicted": [1.0, 5.0, None]})

        metrics = component_regression_metrics(
            frame, actual_column="actual", predicted_column="predicted"
        )

        assert metrics["n"] == 1
        assert metrics["mae"] == pytest.approx(0.0)

    def test_poisson_deviance_is_zero_for_a_perfect_prediction(self) -> None:
        frame = pl.DataFrame({"actual": [0.0, 1.0, 3.0], "predicted": [0.0, 1.0, 3.0]})

        metrics = component_regression_metrics(
            frame, actual_column="actual", predicted_column="predicted", poisson=True
        )

        assert metrics["poisson_deviance"] == pytest.approx(0.0, abs=1e-6)

    def test_empty_frame_returns_all_none(self) -> None:
        frame = pl.DataFrame(
            {"actual": [None], "predicted": [None]},
            schema={"actual": pl.Float64, "predicted": pl.Float64},
        )

        metrics = component_regression_metrics(
            frame, actual_column="actual", predicted_column="predicted"
        )

        assert metrics == {"mae": None, "rmse": None, "poisson_deviance": None, "n": 0}


def _perfect_prediction_row(*, season: str, position: str) -> dict:
    """A row whose actual scoring inputs are known, with every predicted
    column set to exactly that actual value - the invariant the plan's
    Step 30 unit test calls for."""
    actual = PlayerFixtureRow(
        position=position,
        minutes=75,
        goals_scored=1,
        assists=1,
        goals_conceded=1,
        own_goals=0,
        penalties_saved=0,
        penalties_missed=0,
        yellow_cards=1,
        red_cards=0,
        saves=0,
        bonus=2,
        cbi=11,
        tackles=0,
        recoveries=0,
    )
    rules = ruleset_for_name(ruleset_name_for_season(season))
    realised_total = rules.points(actual).total
    return {
        "season": season,
        "event": 1,
        "position": position,
        "label_total_points_fpl": float(realised_total),
        "glm_minutes": float(actual.minutes),
        "glm_goals_scored": float(actual.goals_scored),
        "glm_assists": float(actual.assists),
        "glm_goals_conceded": float(actual.goals_conceded),
        "glm_bonus": float(actual.bonus),
        "glm_defensive_contribution": float(actual.cbi),
        "naive_saves": float(actual.saves),
        "naive_yellow_cards": float(actual.yellow_cards),
        "naive_red_cards": float(actual.red_cards),
        "naive_penalties_saved": float(actual.penalties_saved),
        "naive_penalties_missed": float(actual.penalties_missed),
        "naive_own_goals": float(actual.own_goals),
    }


class TestAssemblePredictedPoints:
    def test_perfect_component_predictions_reproduce_realised_total(self) -> None:
        frame = pl.DataFrame(
            [
                _perfect_prediction_row(season="2016-17", position="MID"),
                _perfect_prediction_row(season="2025-26", position="DEF"),
                _perfect_prediction_row(season="2026-27", position="FWD"),
            ]
        )

        result = assemble_predicted_points(frame)

        assert result["predicted_total_points_fpl"].to_list() == pytest.approx(
            result["label_total_points_fpl"].to_list()
        )

    def test_a_row_with_any_null_prediction_gets_a_null_total(self) -> None:
        row = _perfect_prediction_row(season="2016-17", position="MID")
        row["naive_saves"] = None
        frame = pl.DataFrame([row])

        result = assemble_predicted_points(frame)

        assert result["predicted_total_points_fpl"].to_list() == [None]

    def test_raises_on_a_missing_required_column(self) -> None:
        frame = pl.DataFrame([{"season": "2016-17", "position": "MID"}])

        with pytest.raises(ValueError, match="missing column"):
            assemble_predicted_points(frame)


class TestPointsErrorReport:
    def test_reports_overall_and_every_outcome_bucket(self) -> None:
        frame = pl.DataFrame(
            {
                "label_total_points_fpl": [0.0, 2.0, 6.0, 12.0],
                "predicted_total_points_fpl": [0.0, 3.0, 5.0, 10.0],
            }
        )

        report = points_error_report(frame)

        assert set(report["bucket"].to_list()) == {"overall", *OUTCOME_BUCKETS}
        zeros_row = report.filter(pl.col("bucket") == "zeros")
        assert zeros_row["n"][0] == 1
        assert zeros_row["mae"][0] == pytest.approx(0.0)


class TestSpearmanByGameweek:
    def test_perfectly_monotonic_predictions_score_a_correlation_of_one(self) -> None:
        frame = pl.DataFrame(
            {
                "season": ["2016-17"] * 3,
                "event": [1, 1, 1],
                "label_total_points_fpl": [1.0, 5.0, 10.0],
                "predicted_total_points_fpl": [2.0, 6.0, 11.0],
            }
        )

        result = spearman_by_gameweek(frame)

        assert result["spearman"].to_list() == pytest.approx([1.0])

    def test_a_gameweek_with_fewer_than_two_players_gets_null(self) -> None:
        frame = pl.DataFrame(
            {
                "season": ["2016-17"],
                "event": [1],
                "label_total_points_fpl": [4.0],
                "predicted_total_points_fpl": [3.0],
            }
        )

        result = spearman_by_gameweek(frame)

        assert result["spearman"].to_list() == [None]
