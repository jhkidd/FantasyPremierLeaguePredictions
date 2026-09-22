"""LightGBM candidate model (`.github/context/gbm-baseline-phase-b.md`,
Phase B §3.2's first candidate against the GLM baseline).

:class:`GbmBaseline` deliberately mirrors :class:`fpl.training.baseline.
GlmBaseline`'s shape exactly: the same two-stage design (a ``minutes``
model per position; a ``label_<component>`` model per (component,
position), fit only on rows the player actually played), the same
:data:`~fpl.training.baseline.GLM_COMPONENTS` set, and the same
``P(play) x E[component|play]`` combination rule in :func:`predict_gbm_baseline`
- so the two bundles are directly comparable via the same evaluation
harness (:mod:`fpl.training.evaluation`), just swapping which predicted
column prefix is read.

The one deliberate divergence: feature selection uses every
:func:`fpl.training.eda.numeric_feature_columns` column, including the
era-masked rolling features :func:`fpl.training.baseline.
primary_feature_columns` excludes (Phase B §3.2's own reasoning) - a
gradient-boosted tree learns an explicit split direction for a missing
value rather than needing an imputed one, so blending a masked feature's
NaN gaps across eras is not the same systematic error it would be for the
GLM baseline's median-imputed linear fit. There is also no
scaling/imputation pipeline step at all: trees are scale-invariant and
handle NaN natively, so neither is needed.

Every hyperparameter below is a fixed, reasonable default for this
dataset's size (position-partitioned training rows range from roughly
2,000-3,000 for GK/FWD to 9,000-13,000 for DEF/MID across ten seasons -
`docs/model-prototype-baseline.md` §3) - conservative enough to resist
overfitting the smaller position partitions, not tuned against any split.
Tuning (Bayesian optimisation over validation) is deliberately deferred to
a later Phase B task (§3.3).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import polars as pl
from lightgbm import LGBMRegressor

from fpl.scoring.base import POSITIONS, Position
from fpl.training.baseline import GLM_COMPONENTS
from fpl.training.eda import numeric_feature_columns

__all__ = [
    "GbmBaseline",
    "fit_gbm_baseline",
    "predict_gbm_baseline",
]

# Shared across both stages: shallow trees, a low learning rate and a
# floor on samples-per-leaf, all chosen to keep a single tree from
# memorising the smallest position partition (~2,000 rows for GK).
_COMMON_PARAMS: dict = {
    "n_estimators": 300,
    "max_depth": 5,
    "num_leaves": 31,
    "learning_rate": 0.03,
    "min_child_samples": 20,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "random_state": 42,
    "verbosity": -1,
}


def _minutes_model() -> LGBMRegressor:
    return LGBMRegressor(objective="regression", **_COMMON_PARAMS)


def _component_model() -> LGBMRegressor:
    # Poisson matches the GLM baseline's own PoissonRegressor link for the
    # same count targets (goals, assists, goals conceded, bonus, defensive
    # contribution), for a like-for-like comparison of link choice held
    # constant while only the estimator family changes.
    return LGBMRegressor(objective="poisson", **_COMMON_PARAMS)


def _feature_columns(frame: pl.DataFrame) -> list[str]:
    """Every :func:`fpl.training.eda.numeric_feature_columns` column with
    at least one observed value in ``frame`` - unlike
    :func:`fpl.training.baseline.primary_feature_columns`, nothing is
    excluded for being era-masked (module docstring). An all-null column
    is still dropped: there is nothing for a tree to split on, and handing
    LightGBM a column of nothing but NaN would waste a split search on it
    every round for no benefit."""
    candidate_columns = numeric_feature_columns(frame)
    return [column for column in candidate_columns if frame[column].null_count() < frame.height]


@dataclass(frozen=True)
class GbmBaseline:
    """A fitted position-specific LightGBM baseline (Phase B §3.2)."""

    feature_columns: list[str]
    minutes_models: dict[Position, LGBMRegressor] = field(default_factory=dict)
    component_models: dict[tuple[str, Position], LGBMRegressor] = field(default_factory=dict)


def _to_numpy(frame: pl.DataFrame, columns: Sequence[str]) -> np.ndarray:
    # Cast first: a rolling/team-context column can be an integer dtype
    # with nulls, and polars' own to_numpy() on an un-cast integer column
    # with nulls raises rather than yielding NaN the way a float column
    # does - casting to Float64 first is what actually gets LightGBM a
    # plain NaN for "missing", not an error.
    return frame.select(columns).cast(pl.Float64).to_numpy()


def fit_gbm_baseline(
    train_frame: pl.DataFrame, *, components: Sequence[str] = GLM_COMPONENTS
) -> GbmBaseline:
    """Fit the two-stage LightGBM baseline on ``train_frame`` (the training
    split only) - same per-position, per-(component, position) structure as
    :func:`fpl.training.baseline.fit_glm_baseline`, see the module
    docstring for what differs and why."""
    feature_columns = _feature_columns(train_frame)
    played = train_frame.filter(pl.col("label_minutes") > 0)

    minutes_models: dict[Position, LGBMRegressor] = {}
    component_models: dict[tuple[str, Position], LGBMRegressor] = {}

    for position in sorted(POSITIONS):
        position_frame = train_frame.filter(pl.col("position") == position)
        if position_frame.height == 0:
            continue

        minutes_model = _minutes_model()
        minutes_model.fit(
            _to_numpy(position_frame, feature_columns),
            position_frame["label_minutes"].to_numpy(),
        )
        minutes_models[position] = minutes_model

        position_played = played.filter(pl.col("position") == position)
        if position_played.height == 0:
            continue
        for component in components:
            label_column = f"label_{component}"
            component_frame = position_played.filter(pl.col(label_column).is_not_null())
            if component_frame.height == 0:
                continue
            component_model = _component_model()
            component_model.fit(
                _to_numpy(component_frame, feature_columns),
                component_frame[label_column].to_numpy(),
            )
            component_models[(component, position)] = component_model

    return GbmBaseline(
        feature_columns=feature_columns,
        minutes_models=minutes_models,
        component_models=component_models,
    )


def predict_gbm_baseline(
    bundle: GbmBaseline, frame: pl.DataFrame, *, components: Sequence[str] = GLM_COMPONENTS
) -> pl.DataFrame:
    """Predict every fitted target for every row of ``frame``, returning
    ``frame`` with one new ``lgbm_<target>`` column per target - same
    ``P(play) x E[component|play]`` combination rule as
    :func:`fpl.training.baseline.predict_glm_baseline`, see that function's
    own docstring for the reasoning. A row whose position has no fitted
    model at all gets ``NaN`` for every target."""
    positions = np.asarray(frame["position"].to_list())
    features = _to_numpy(frame, bundle.feature_columns)

    predicted_minutes = np.full(frame.height, np.nan)
    play_fraction = np.full(frame.height, np.nan)
    for position, model in bundle.minutes_models.items():
        mask = positions == position
        if not mask.any():
            continue
        predicted = model.predict(features[mask])
        predicted_minutes[mask] = predicted
        play_fraction[mask] = np.clip(predicted / 90.0, 0.0, 1.0)

    result_columns: dict[str, np.ndarray] = {"lgbm_minutes": predicted_minutes}

    for component in components:
        component_prediction = np.full(frame.height, np.nan)
        for position in POSITIONS:
            model = bundle.component_models.get((component, position))
            if model is None:
                continue
            mask = positions == position
            if not mask.any():
                continue
            component_prediction[mask] = model.predict(features[mask])
        result_columns[f"lgbm_{component}"] = play_fraction * component_prediction

    return frame.with_columns(
        [
            pl.Series(name, values.tolist(), dtype=pl.Float64)
            for name, values in result_columns.items()
        ]
    )
