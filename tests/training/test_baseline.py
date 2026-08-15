"""Tests for :mod:`fpl.training.baseline`'s naive rolling-mean floor
(Phase A Step 28) and GLM baseline (Phase A Step 29)."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from fpl.scoring.base import POSITIONS
from fpl.training.baseline import (
    GLM_COMPONENTS,
    fit_glm_baseline,
    naive_rolling_mean_predictions,
    predict_glm_baseline,
    primary_feature_columns,
)
from fpl.training.dataset import LABEL_COLUMNS


def _row(*, season: str, event: int, fixture_id: int, player_id: int, goals: float) -> dict:
    row: dict = {
        "season": season,
        "event": event,
        "fixture_id": fixture_id,
        "player_id": player_id,
        "player_code": f"code-{player_id}",
    }
    for label in LABEL_COLUMNS:
        row[label] = 0.0
    row["label_goals_scored"] = goals
    return row


def test_first_ever_row_has_null_naive_prediction() -> None:
    frame = pl.DataFrame([_row(season="2016-17", event=1, fixture_id=1, player_id=1, goals=1.0)])

    result = naive_rolling_mean_predictions(frame, targets=["goals_scored"])

    assert result["naive_goals_scored"].to_list() == [None]


def test_second_row_naive_prediction_is_prior_row_mean() -> None:
    frame = pl.DataFrame(
        [
            _row(season="2016-17", event=1, fixture_id=1, player_id=1, goals=2.0),
            _row(season="2016-17", event=2, fixture_id=2, player_id=1, goals=0.0),
        ]
    )

    result = naive_rolling_mean_predictions(frame, targets=["goals_scored"])

    assert result.sort("event")["naive_goals_scored"].to_list() == [None, 2.0]


def test_window_truncates_to_most_recent_fixtures() -> None:
    rows = [
        _row(season="2016-17", event=event, fixture_id=event, player_id=1, goals=float(event))
        for event in range(1, 8)
    ]
    frame = pl.DataFrame(rows)

    result = naive_rolling_mean_predictions(frame, targets=["goals_scored"], window=3)

    # Event 7's naive prediction is the mean of events 4, 5, 6's realised
    # goals (3-fixture trailing window), never event 7's own value.
    row_7 = result.filter(pl.col("event") == 7)
    assert row_7["naive_goals_scored"].to_list() == [(4.0 + 5.0 + 6.0) / 3]


def test_double_gameweek_rows_share_history_not_each_other() -> None:
    frame = pl.DataFrame(
        [
            _row(season="2016-17", event=1, fixture_id=1, player_id=1, goals=1.0),
            _row(season="2016-17", event=2, fixture_id=2, player_id=1, goals=5.0),
            _row(season="2016-17", event=2, fixture_id=3, player_id=1, goals=9.0),
            _row(season="2016-17", event=3, fixture_id=4, player_id=1, goals=0.0),
        ]
    )

    result = naive_rolling_mean_predictions(frame, targets=["goals_scored"], window=5)

    event_2 = result.filter(pl.col("event") == 2).sort("fixture_id")
    # Both of event 2's fixtures see only event 1's history (1.0) - neither
    # sees the other's own goals.
    assert event_2["naive_goals_scored"].to_list() == [1.0, 1.0]

    event_3 = result.filter(pl.col("event") == 3)
    # Event 3 sees both of event 2's fixtures plus event 1: mean(1, 5, 9).
    assert event_3["naive_goals_scored"].to_list() == [(1.0 + 5.0 + 9.0) / 3]


def test_season_boundary_resets_history() -> None:
    frame = pl.DataFrame(
        [
            _row(season="2016-17", event=38, fixture_id=1, player_id=1, goals=3.0),
            _row(season="2017-18", event=1, fixture_id=2, player_id=1, goals=0.0),
        ]
    )

    result = naive_rolling_mean_predictions(frame, targets=["goals_scored"])

    next_season_row = result.filter(pl.col("season") == "2017-18")
    assert next_season_row["naive_goals_scored"].to_list() == [None]


def test_defaults_to_every_label_column() -> None:
    frame = pl.DataFrame([_row(season="2016-17", event=1, fixture_id=1, player_id=1, goals=1.0)])

    result = naive_rolling_mean_predictions(frame)

    for label in LABEL_COLUMNS:
        assert f"naive_{label[len('label_') :]}" in result.columns


def test_null_realised_values_are_excluded_from_the_window() -> None:
    rows = [
        _row(season="2016-17", event=1, fixture_id=1, player_id=1, goals=2.0),
        _row(season="2016-17", event=2, fixture_id=2, player_id=1, goals=4.0),
    ]
    frame = pl.DataFrame(rows).with_columns(
        pl.when(pl.col("event") == 1)
        .then(None)
        .otherwise(pl.col("label_goals_scored"))
        .alias("label_goals_scored")
    )

    result = naive_rolling_mean_predictions(frame, targets=["goals_scored"])

    row_2 = result.filter(pl.col("event") == 2)
    # Event 1's null is excluded entirely rather than treated as 0.
    assert row_2["naive_goals_scored"].to_list() == [None]


def _glm_frame(*, seed: int = 0, n_per_position: int = 40) -> pl.DataFrame:
    """A synthetic frame shaped like the training matrix, big and varied
    enough per position for Ridge/Poisson to fit without a convergence
    warning: two ordinary numeric features (``feature_a``/``feature_b``,
    correlated with whether a player plays and how much they contribute)
    plus one era-masked feature (``cbi_sum_last_3``, governed by
    ``obs_defensive``) that :func:`primary_feature_columns` must exclude."""
    from fpl.scoring.base import POSITIONS

    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    for position in sorted(POSITIONS):
        for _ in range(n_per_position):
            feature_a = rng.normal()
            feature_b = rng.normal()
            played = rng.random() > 0.2
            minutes = float(rng.integers(60, 90)) if played else 0.0
            rows.append(
                {
                    "position": position,
                    "feature_a": feature_a,
                    "feature_b": feature_b,
                    "cbi_sum_last_3": feature_a,
                    "obs_defensive": True,
                    "obs_bps_inputs": True,
                    "obs_expected": True,
                    "obs_starts": True,
                    "label_minutes": minutes,
                    "label_goals_scored": float(rng.poisson(0.3)) if played else 0.0,
                    "label_assists": float(rng.poisson(0.2)) if played else 0.0,
                    "label_goals_conceded": float(rng.poisson(1.0)) if played else 0.0,
                    "label_bonus": float(rng.poisson(0.5)) if played else 0.0,
                    "label_defensive_contribution": float(rng.poisson(2.0)) if played else 0.0,
                }
            )
    return pl.DataFrame(rows)


class TestPrimaryFeatureColumns:
    def test_excludes_masked_columns_but_keeps_ordinary_numeric_columns(self) -> None:
        frame = _glm_frame()

        columns = primary_feature_columns(frame)

        assert "feature_a" in columns
        assert "feature_b" in columns
        assert "cbi_sum_last_3" not in columns


class TestFitGlmBaseline:
    def test_fits_one_minutes_model_per_position_present(self) -> None:
        frame = _glm_frame()

        bundle = fit_glm_baseline(frame)

        assert set(bundle.minutes_models) == POSITIONS
        assert bundle.excluded_masked_columns == ["cbi_sum_last_3"]

    def test_fits_one_component_model_per_position_and_component(self) -> None:
        frame = _glm_frame()

        bundle = fit_glm_baseline(frame)

        for position in POSITIONS:
            for component in GLM_COMPONENTS:
                assert (component, position) in bundle.component_models

    def test_component_with_all_null_labels_for_a_position_has_no_model(self) -> None:
        """`defensive_contribution` is null for every row before the
        2025-26 season it was introduced in (real-data finding from Step
        32) - a training split entirely predating that rule must not
        crash trying to fit a PoissonRegressor on an all-null target, and
        must simply have no entry for that (component, position) pair, so
        `predict_glm_baseline` falls back to its existing None/NaN
        contract for it."""
        frame = _glm_frame().with_columns(
            pl.lit(None, dtype=pl.Float64).alias("label_defensive_contribution")
        )

        bundle = fit_glm_baseline(frame)

        for position in POSITIONS:
            assert ("defensive_contribution", position) not in bundle.component_models
        # Every other component is unaffected.
        for position in POSITIONS:
            for component in [c for c in GLM_COMPONENTS if c != "defensive_contribution"]:
                assert (component, position) in bundle.component_models

    def test_component_with_partially_null_labels_fits_on_non_null_rows_only(self) -> None:
        frame = _glm_frame()
        # Null out one row's defensive_contribution label per position -
        # the fit must still succeed and simply skip that row.
        mutated = frame.with_columns(
            pl.when(pl.int_range(pl.len()).over("position") == 0)
            .then(None)
            .otherwise(pl.col("label_defensive_contribution"))
            .alias("label_defensive_contribution")
        )

        bundle = fit_glm_baseline(mutated)

        for position in POSITIONS:
            assert ("defensive_contribution", position) in bundle.component_models

    def test_component_models_are_unaffected_by_zero_minute_row_targets(self) -> None:
        """A row where `label_minutes == 0` has no meaningful component
        rate - changing its target value must not move a component
        model's fitted predictions on the rows that did play."""
        frame = _glm_frame()
        mutated = frame.with_columns(
            pl.when(pl.col("label_minutes") == 0)
            .then(999.0)
            .otherwise(pl.col("label_goals_scored"))
            .alias("label_goals_scored")
        )

        bundle_original = fit_glm_baseline(frame)
        bundle_mutated = fit_glm_baseline(mutated)

        played = frame.filter(pl.col("label_minutes") > 0)
        predictions_original = predict_glm_baseline(bundle_original, played)
        predictions_mutated = predict_glm_baseline(bundle_mutated, played)

        assert predictions_original["glm_goals_scored"].to_list() == pytest.approx(
            predictions_mutated["glm_goals_scored"].to_list()
        )


class TestPredictGlmBaseline:
    def test_play_fraction_derived_prediction_is_bounded(self) -> None:
        frame = _glm_frame()
        bundle = fit_glm_baseline(frame)

        predictions = predict_glm_baseline(bundle, frame)

        # glm_minutes / 90 clipped to [0, 1] is the P(play) factor - so
        # every component prediction must be non-negative (Poisson) times
        # a fraction in [0, 1], and can never be negative itself.
        for component in GLM_COMPONENTS:
            values = predictions[f"glm_{component}"].drop_nulls().to_list()
            assert all(value >= -1e-9 for value in values)

    def test_predicts_every_fitted_target_column(self) -> None:
        frame = _glm_frame()
        bundle = fit_glm_baseline(frame)

        predictions = predict_glm_baseline(bundle, frame)

        assert "glm_minutes" in predictions.columns
        for component in GLM_COMPONENTS:
            assert f"glm_{component}" in predictions.columns
        assert predictions["glm_minutes"].null_count() == 0
