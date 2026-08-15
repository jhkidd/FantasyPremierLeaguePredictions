"""Baseline predictors for the training matrix (plan Phase A Steps 28-29).

Step 28's naive floor: for each target, predict a player's own trailing
arithmetic mean over their most recent fixtures - the simplest possible
"nothing learned, just persistence" benchmark every later model must beat
(plan Q11).

The windowing semantics deliberately mirror
:func:`fpl.training.dataset.build_training_matrix`'s own leakage-safety
invariant exactly, since a baseline that leaked information it shouldn't
have would be comparing against a false floor:

- History accumulates one gameweek (``event``) at a time, never a fixture
  at a time - two fixtures in the same gameweek (a double gameweek) share
  identical history and never see each other's realised value.
- History resets at every season boundary, matching
  :func:`fpl.training.dataset._build_one_season` resetting its own
  ``history_so_far`` per season (the fixed fixture-count windows in
  practice never span seasons in the current implementation, despite
  :mod:`fpl.features.rolling`'s docstring describing the general intent -
  the naive baseline stays consistent with what the real features actually
  do, not with what they are eventually meant to do).
- A null realised value is excluded from the window entirely, never
  treated as zero.

Step 29's GLM baseline: one scikit-learn ``Pipeline`` per (component,
position) - ``SimpleImputer(strategy="median", add_indicator=True)`` ->
``StandardScaler`` -> an estimator matched to the target's link
(:class:`~sklearn.linear_model.Ridge` for ``minutes``,
:class:`~sklearn.linear_model.PoissonRegressor` for every count target in
:data:`GLM_COMPONENTS`), following OpenFPL's position-specific-model
convention (plan Q24). Two-stage per plan Q26: the minutes model trains on
every row; every component model trains only on rows the player actually
played (``label_minutes > 0``), and a prediction is
``P(play) x E[component | play]`` - see :func:`predict_glm_baseline` for
how ``P(play)`` is derived from the single fitted minutes model. Team
one-hots are never in the feature set at all (plan Q27 - elo/odds/
congestion already carry team strength), and every era-masked rolling
feature is excluded from this primary fit (plan Q22 - see
:func:`primary_feature_columns`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import polars as pl
from sklearn.impute import SimpleImputer
from sklearn.linear_model import PoissonRegressor, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from fpl.features.rolling import FIXTURE_WINDOWS
from fpl.scoring.base import POSITIONS, Position
from fpl.training.dataset import LABEL_COLUMNS
from fpl.training.eda import masked_feature_obs_column, numeric_feature_columns

__all__ = [
    "DEFAULT_NAIVE_WINDOW",
    "GLM_COMPONENTS",
    "MINUTES_TARGET",
    "GlmBaseline",
    "fit_glm_baseline",
    "naive_rolling_mean_predictions",
    "predict_glm_baseline",
    "primary_feature_columns",
]

# "Last 5 fixtures" is the standard current-form window - the middle of the
# same fixed candidate set (3/5/10) the real rolling features use.
DEFAULT_NAIVE_WINDOW: int = FIXTURE_WINDOWS[1]

MINUTES_TARGET: str = "minutes"

# The count targets fit directly (plan Q34). Saves, cards, penalties and
# own goals stay naive-only (plan Q20) - they are not in this tuple and
# fit_glm_baseline never builds a model for them.
GLM_COMPONENTS: tuple[str, ...] = (
    "goals_scored",
    "assists",
    "goals_conceded",
    "bonus",
    "defensive_contribution",
)


def naive_rolling_mean_predictions(
    frame: pl.DataFrame,
    *,
    targets: Sequence[str] | None = None,
    window: int = DEFAULT_NAIVE_WINDOW,
) -> pl.DataFrame:
    """Return ``frame`` with one new ``naive_<target>`` column per target in
    ``targets`` (default: every :data:`fpl.training.dataset.LABEL_COLUMNS`
    name) - the unweighted mean of that player's own last ``window``
    realised fixtures, strictly before the current gameweek.

    A player's very first row of a season always gets ``None`` (no history
    yet). ``frame`` must already have ``season``, ``event``, ``player_id``
    and a ``label_<target>`` column for every requested target - i.e. it is
    shaped like :func:`fpl.training.dataset.build_training_matrix`'s output,
    though any row subset/order is accepted since this function does its own
    sort.
    """
    # LABEL_COLUMNS entries are already "label_"-prefixed (e.g.
    # "label_minutes"); every target name used below - explicit or default
    # - must be the short, unprefixed form so `f"label_{target}"` resolves
    # to a real column instead of double-prefixing.
    target_columns = (
        list(targets)
        if targets is not None
        else [label[len("label_") :] for label in LABEL_COLUMNS]
    )
    output_columns = {target: f"naive_{target}" for target in target_columns}

    pieces = []
    for _keys, group in frame.sort("event").group_by(["season", "player_id"], maintain_order=True):
        history: dict[str, list[float]] = {target: [] for target in target_columns}
        naive_values: dict[str, list[float | None]] = {target: [] for target in target_columns}

        for _event, event_group in group.group_by("event", maintain_order=True):
            for target in target_columns:
                window_values = history[target][-window:]
                mean_value = sum(window_values) / len(window_values) if window_values else None
                naive_values[target].extend([mean_value] * event_group.height)

            for target in target_columns:
                realised = event_group[f"label_{target}"].to_list()
                history[target].extend(value for value in realised if value is not None)

        pieces.append(
            group.with_columns(
                [
                    pl.Series(output_columns[target], naive_values[target], dtype=pl.Float64)
                    for target in target_columns
                ]
            )
        )

    result = pl.concat(pieces, how="vertical")
    sort_keys = [
        key for key in ("season", "event", "player_id", "fixture_id") if key in result.columns
    ]
    return result.sort(sort_keys) if sort_keys else result


def primary_feature_columns(frame: pl.DataFrame) -> list[str]:
    """Every :func:`fpl.training.eda.numeric_feature_columns` column
    eligible for the primary GLM fit - every one of them *except* a
    rolling feature governed by an ``obs_*`` era mask (plan Q22).
    Blending a masked feature across eras via ordinary median-imputation
    would average an era that never recorded the stat together with one
    that did; :attr:`GlmBaseline.excluded_masked_columns` keeps the
    excluded list around for the "reported separately" half of that
    answer."""
    masked = set(masked_feature_obs_column(frame))
    return [column for column in numeric_feature_columns(frame) if column not in masked]


def _pipeline(estimator: Ridge | PoissonRegressor) -> Pipeline:
    return Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
            ("estimate", estimator),
        ]
    )


@dataclass(frozen=True)
class GlmBaseline:
    """A fitted position-specific GLM baseline (plan Phase A Step 29)."""

    feature_columns: list[str]
    excluded_masked_columns: list[str]
    minutes_models: dict[Position, Pipeline] = field(default_factory=dict)
    component_models: dict[tuple[str, Position], Pipeline] = field(default_factory=dict)


def fit_glm_baseline(
    train_frame: pl.DataFrame, *, components: Sequence[str] = GLM_COMPONENTS
) -> GlmBaseline:
    """Fit the two-stage GLM baseline on ``train_frame`` (the training
    split only).

    One :class:`~sklearn.linear_model.Ridge` pipeline is fit per position
    for ``minutes``, on every row of that position. One
    :class:`~sklearn.linear_model.PoissonRegressor` pipeline is fit per
    (component, position) for every name in ``components``, on only that
    position's rows with ``label_minutes > 0`` (plan Q26) - a player who
    did not play has no meaningful rate to learn a component model from,
    and keeping 0-minute rows in would bias every component toward zero
    for reasons that have nothing to do with the feature set. A position
    absent from ``train_frame`` (or with no rows where anyone played)
    simply has no entry in the returned mappings.

    A :func:`primary_feature_columns` column with zero observed values in
    ``train_frame`` (e.g. a team-context feature no season in this training
    split ever populated) is dropped from the fit entirely rather than
    handed to ``SimpleImputer`` - there is nothing to impute a median from,
    and scikit-learn only warns and silently drops it anyway, which would
    make :attr:`GlmBaseline.feature_columns` an inaccurate record of what
    the fitted pipelines actually used.
    """
    candidate_feature_columns = primary_feature_columns(train_frame)
    feature_columns = [
        column
        for column in candidate_feature_columns
        if train_frame[column].null_count() < train_frame.height
    ]
    excluded = sorted(masked_feature_obs_column(train_frame))
    played = train_frame.filter(pl.col("label_minutes") > 0)

    minutes_models: dict[Position, Pipeline] = {}
    component_models: dict[tuple[str, Position], Pipeline] = {}

    for position in sorted(POSITIONS):
        position_frame = train_frame.filter(pl.col("position") == position)
        if position_frame.height == 0:
            continue

        minutes_pipeline = _pipeline(Ridge())
        minutes_pipeline.fit(
            position_frame.select(feature_columns).to_numpy(),
            position_frame["label_minutes"].to_numpy(),
        )
        minutes_models[position] = minutes_pipeline

        position_played = played.filter(pl.col("position") == position)
        if position_played.height == 0:
            continue
        played_features = position_played.select(feature_columns).to_numpy()
        for component in components:
            component_pipeline = _pipeline(PoissonRegressor())
            component_pipeline.fit(
                played_features, position_played[f"label_{component}"].to_numpy()
            )
            component_models[(component, position)] = component_pipeline

    return GlmBaseline(
        feature_columns=feature_columns,
        excluded_masked_columns=excluded,
        minutes_models=minutes_models,
        component_models=component_models,
    )


def predict_glm_baseline(
    bundle: GlmBaseline, frame: pl.DataFrame, *, components: Sequence[str] = GLM_COMPONENTS
) -> pl.DataFrame:
    """Predict every fitted target for every row of ``frame``, returning
    ``frame`` with one new ``glm_<target>`` column per target.

    ``glm_minutes`` is the raw Ridge prediction for that row's position.
    Every other ``glm_<component>`` is ``P(play) x E[component | play]``
    (plan Q26): ``P(play)`` is the Ridge-predicted minutes expressed as a
    fraction of a full 90 and clipped to ``[0, 1]`` - the plan fits only
    one minutes model rather than a separate play/no-play classifier, so
    this is the natural continuous stand-in for "probability of playing"
    the two-stage combination calls for. A row whose position has no
    fitted model at all (e.g. absent from the training split) gets
    ``None`` for every target.
    """
    positions = np.asarray(frame["position"].to_list())
    features = frame.select(bundle.feature_columns).to_numpy()

    predicted_minutes = np.full(frame.height, np.nan)
    play_fraction = np.full(frame.height, np.nan)
    for position, pipeline in bundle.minutes_models.items():
        mask = positions == position
        if not mask.any():
            continue
        predicted = pipeline.predict(features[mask])
        predicted_minutes[mask] = predicted
        play_fraction[mask] = np.clip(predicted / 90.0, 0.0, 1.0)

    result_columns: dict[str, np.ndarray] = {"glm_minutes": predicted_minutes}

    for component in components:
        component_prediction = np.full(frame.height, np.nan)
        for position in POSITIONS:
            pipeline = bundle.component_models.get((component, position))
            if pipeline is None:
                continue
            mask = positions == position
            if not mask.any():
                continue
            component_prediction[mask] = pipeline.predict(features[mask])
        result_columns[f"glm_{component}"] = play_fraction * component_prediction

    return frame.with_columns(
        [
            pl.Series(name, values.tolist(), dtype=pl.Float64)
            for name, values in result_columns.items()
        ]
    )
