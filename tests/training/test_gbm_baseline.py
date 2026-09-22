"""Tests for :mod:`fpl.training.gbm_baseline` — the first Phase B candidate
model (`.github/context/gbm-baseline-phase-b.md`).

Mirrors :mod:`tests.training.test_baseline`'s ``GlmBaseline`` test shape,
since :class:`~fpl.training.gbm_baseline.GbmBaseline` deliberately mirrors
:class:`~fpl.training.baseline.GlmBaseline`'s two-stage, per-position design
- same ``fit``/``predict`` contract, same ``P(play) x E[component|play]``
combination rule - so the two are directly comparable. The one deliberate
difference under test here is the feature set: this module uses every
:func:`fpl.training.eda.numeric_feature_columns` column, including the
era-masked ones :func:`fpl.training.baseline.primary_feature_columns`
excludes, since LightGBM handles missing values natively.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from fpl.scoring.base import POSITIONS
from fpl.training.baseline import GLM_COMPONENTS
from fpl.training.gbm_baseline import fit_gbm_baseline, predict_gbm_baseline


def _gbm_frame(*, seed: int = 0, n_per_position: int = 60) -> pl.DataFrame:
    """A synthetic frame shaped like the training matrix - the same shape
    :mod:`tests.training.test_baseline`'s own ``_glm_frame`` builds, with
    more rows per position since a tree ensemble wants more data per leaf
    than a linear model to fit without instability."""
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
                    # Era-masked in the real training matrix (governed by
                    # obs_defensive) - GLM excludes this; the GBM must not.
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


class TestFitGbmBaseline:
    def test_fits_one_minutes_model_per_position_present(self) -> None:
        frame = _gbm_frame()

        bundle = fit_gbm_baseline(frame)

        assert set(bundle.minutes_models) == POSITIONS

    def test_fits_one_component_model_per_position_and_component(self) -> None:
        frame = _gbm_frame()

        bundle = fit_gbm_baseline(frame)

        for position in POSITIONS:
            for component in GLM_COMPONENTS:
                assert (component, position) in bundle.component_models

    def test_feature_set_includes_era_masked_columns(self) -> None:
        """The one deliberate divergence from the GLM baseline under test:
        `cbi_sum_last_3` is era-masked (governed by `obs_defensive`) and
        `fit_glm_baseline` excludes it, but this module must keep it."""
        frame = _gbm_frame()

        bundle = fit_gbm_baseline(frame)

        assert "cbi_sum_last_3" in bundle.feature_columns
        assert "feature_a" in bundle.feature_columns
        assert "feature_b" in bundle.feature_columns

    def test_component_with_all_null_labels_for_a_position_has_no_model(self) -> None:
        """`defensive_contribution` is null for every row before the
        2025-26 season it was introduced in - a training split entirely
        predating that rule must not crash fitting an all-null target, and
        must simply have no entry for that (component, position) pair."""
        frame = _gbm_frame().with_columns(
            pl.lit(None, dtype=pl.Float64).alias("label_defensive_contribution")
        )

        bundle = fit_gbm_baseline(frame)

        for position in POSITIONS:
            assert ("defensive_contribution", position) not in bundle.component_models
        for position in POSITIONS:
            for component in [c for c in GLM_COMPONENTS if c != "defensive_contribution"]:
                assert (component, position) in bundle.component_models

    def test_component_with_partially_null_labels_fits_on_non_null_rows_only(self) -> None:
        frame = _gbm_frame()
        mutated = frame.with_columns(
            pl.when(pl.int_range(pl.len()).over("position") == 0)
            .then(None)
            .otherwise(pl.col("label_defensive_contribution"))
            .alias("label_defensive_contribution")
        )

        bundle = fit_gbm_baseline(mutated)

        for position in POSITIONS:
            assert ("defensive_contribution", position) in bundle.component_models

    def test_all_null_feature_column_is_dropped_rather_than_fitted_on(self) -> None:
        frame = _gbm_frame().with_columns(pl.lit(None, dtype=pl.Float64).alias("feature_c"))

        bundle = fit_gbm_baseline(frame)

        assert "feature_c" not in bundle.feature_columns


class TestPredictGbmBaseline:
    def test_predicts_every_fitted_target_column(self) -> None:
        frame = _gbm_frame()
        bundle = fit_gbm_baseline(frame)

        predictions = predict_gbm_baseline(bundle, frame)

        assert "lgbm_minutes" in predictions.columns
        for component in GLM_COMPONENTS:
            assert f"lgbm_{component}" in predictions.columns
        assert predictions["lgbm_minutes"].null_count() == 0

    def test_play_fraction_derived_prediction_is_non_negative(self) -> None:
        frame = _gbm_frame()
        bundle = fit_gbm_baseline(frame)

        predictions = predict_gbm_baseline(bundle, frame)

        for component in GLM_COMPONENTS:
            values = predictions[f"lgbm_{component}"].drop_nulls().to_list()
            assert all(value >= -1e-9 for value in values)

    def test_a_position_absent_from_training_has_no_predictions(self) -> None:
        frame = _gbm_frame()
        bundle = fit_gbm_baseline(frame.filter(pl.col("position") != "GK"))

        predictions = predict_gbm_baseline(bundle, frame)

        gk_rows = predictions.filter(pl.col("position") == "GK")
        assert all(np.isnan(value) for value in gk_rows["lgbm_minutes"].to_list())

    def test_component_models_are_unaffected_by_zero_minute_row_targets(self) -> None:
        """Mirrors the GLM baseline's own equivalent test: a component
        model must be fit only on rows the player actually played, so
        mutating a non-played row's label must not move predictions for
        rows that did play."""
        frame = _gbm_frame()
        mutated = frame.with_columns(
            pl.when(pl.col("label_minutes") == 0)
            .then(999.0)
            .otherwise(pl.col("label_goals_scored"))
            .alias("label_goals_scored")
        )

        bundle_original = fit_gbm_baseline(frame)
        bundle_mutated = fit_gbm_baseline(mutated)

        played = frame.filter(pl.col("label_minutes") > 0)
        predictions_original = predict_gbm_baseline(bundle_original, played)
        predictions_mutated = predict_gbm_baseline(bundle_mutated, played)

        assert predictions_original["lgbm_goals_scored"].to_list() == pytest.approx(
            predictions_mutated["lgbm_goals_scored"].to_list()
        )
