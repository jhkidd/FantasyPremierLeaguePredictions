"""Evaluation metrics for the Phase A baselines (plan Phase A Step 30).

Two layers of metric, both computed **on the validation split only**
(:func:`fpl.training.splits.chronological_split`'s second element) — the
same leakage boundary :mod:`fpl.training.eda` enforces, since choosing
between baselines using test-split numbers would compromise Step 32's real
final read of test performance later:

- **Per-component regression metrics** (:func:`component_regression_metrics`)
  — MAE, RMSE, and (for count targets) Poisson deviance between one
  predicted column and its realised ``label_<target>``.
- **System metric** (:func:`assemble_predicted_points`) — every predicted
  component combined into one predicted ``total_points_fpl`` per row,
  through *that row's own season's* scoring ruleset
  (:mod:`fpl.scoring.rules_legacy`, :mod:`fpl.scoring.rules_2025_26`,
  :mod:`fpl.scoring.rules_2026_27`) so clean sheet, the defensive-
  contribution threshold, and every points term are the same shared
  arithmetic (:mod:`fpl.scoring.base`) the real facts pipeline uses — never
  reimplemented here. :func:`points_error_report` and
  :func:`spearman_by_gameweek` then summarise that predicted total against
  the realised one, overall, per outcome bucket, and per gameweek.
"""

from __future__ import annotations

import numpy as np
import polars as pl
from scipy import stats as scipy_stats

from fpl.config import Season
from fpl.facts import ruleset_for_name
from fpl.scoring.base import PlayerFixtureRow
from fpl.training.baseline import GLM_COMPONENTS

__all__ = [
    "OUTCOME_BUCKETS",
    "assemble_predicted_points",
    "component_regression_metrics",
    "outcome_bucket",
    "points_error_report",
    "ruleset_name_for_season",
    "spearman_by_gameweek",
]

# Plan Q10's bucket thresholds, in ascending order.
OUTCOME_BUCKETS: tuple[str, ...] = ("zeros", "blanks", "tickers", "haulers")

# The scoring inputs GLM never models (plan Q20) - the naive baseline is
# their only predictor, fed straight into the system-score assembly.
_NAIVE_ONLY_COMPONENTS: tuple[str, ...] = (
    "saves",
    "yellow_cards",
    "red_cards",
    "penalties_saved",
    "penalties_missed",
    "own_goals",
)


def ruleset_name_for_season(season: str) -> str:
    """The scoring ruleset name one training-matrix ``season`` string
    (e.g. ``"2016-17"``) reconciles against: ``"legacy"`` before 2025/26,
    ``"2025-26"`` for that season, ``"2026-27"`` from then on - the same
    boundary as :func:`fpl.cli._rules_for_season`, generalised to also
    resolve 2026/27 rather than defaulting it to legacy."""
    start_year = Season.parse(season).start_year
    if start_year < 2025:
        return "legacy"
    if start_year == 2025:
        return "2025-26"
    return "2026-27"


def outcome_bucket(total_points: float) -> str:
    """Plan Q10's answer verbatim: zeros = 0, blanks = 1-3, tickers = 4-8,
    haulers = 9+."""
    if total_points <= 0:
        return "zeros"
    if total_points <= 3:
        return "blanks"
    if total_points <= 8:
        return "tickers"
    return "haulers"


def component_regression_metrics(
    frame: pl.DataFrame,
    *,
    actual_column: str,
    predicted_column: str,
    poisson: bool = False,
) -> dict[str, float | int | None]:
    """MAE and RMSE between ``actual_column`` and ``predicted_column``,
    plus Poisson deviance when ``poisson=True`` (plan Step 30's "count
    targets" - :data:`fpl.training.baseline.GLM_COMPONENTS`).

    A row where either value is null *or* NaN is excluded rather than
    counted as an error - a null/NaN prediction (e.g. a player's
    first-ever fixture with no rolling history yet, or a position with no
    fitted model at all - :func:`fpl.training.baseline.predict_glm_baseline`
    fills exactly this case with ``NaN``, not null) is a structural gap in
    the baseline's coverage, not a wrong guess, and averaging it in as an
    error would penalise the baseline twice for the same missing history
    (worse, silently NaN-poisoning the whole aggregate, since ``NaN`` -
    unlike null - survives ``drop_nulls()`` and propagates through any
    later ``mean``/``sqrt``).
    """
    paired = (
        frame.select(actual_column, predicted_column)
        .drop_nulls()
        .filter(pl.col(actual_column).is_not_nan() & pl.col(predicted_column).is_not_nan())
    )
    if paired.height == 0:
        return {"mae": None, "rmse": None, "poisson_deviance": None, "n": 0}

    actual = paired[actual_column].to_numpy().astype(float)
    predicted = paired[predicted_column].to_numpy().astype(float)
    errors = predicted - actual
    mae = float(np.mean(np.abs(errors)))
    rmse = float(np.sqrt(np.mean(errors**2)))

    deviance = None
    if poisson:
        # Per-observation Poisson deviance, 2*(y*ln(y/mu) - (y-mu)), using
        # the standard y*ln(y/mu) := 0 convention at y=0. mu is clipped
        # away from 0 since a fitted PoissonRegressor's own inverse-link
        # never returns exactly 0 but this metric must stay finite even if
        # a future predictor did.
        safe_predicted = np.clip(predicted, 1e-9, None)
        safe_actual = np.clip(actual, 1e-9, None)
        log_term = np.where(actual > 0, actual * np.log(safe_actual / safe_predicted), 0.0)
        deviance = float(2.0 * np.mean(log_term - (actual - predicted)))

    return {"mae": mae, "rmse": rmse, "poisson_deviance": deviance, "n": paired.height}


def assemble_predicted_points(
    frame: pl.DataFrame,
    *,
    glm_components: tuple[str, ...] = GLM_COMPONENTS,
    naive_components: tuple[str, ...] = _NAIVE_ONLY_COMPONENTS,
) -> pl.DataFrame:
    """Return ``frame`` with one new ``predicted_total_points_fpl`` column:
    every predicted component - ``glm_minutes`` and a ``glm_<component>``
    for each of ``glm_components``, a ``naive_<component>`` for each of
    ``naive_components`` - combined through that row's own season's ruleset
    (:func:`ruleset_name_for_season`).

    The single ``glm_defensive_contribution`` prediction is passed as
    ``PlayerFixtureRow.cbi`` with ``tackles``/``recoveries`` at 0: the
    training matrix's own ``label_defensive_contribution`` is already the
    position-dependent combined sum (``cbi + tackles`` for defenders,
    ``cbi + tackles + recoveries`` for midfielders/forwards -
    :mod:`fpl.quality.checks`'s own formula gate), so
    :func:`fpl.scoring.base.defensive_contribution_points`'s threshold
    check on that single combined value is identical to checking the three
    real components separately.

    A row missing any required prediction (e.g. a player's still-null
    first-fixture naive prediction, or a GLM component with no fitted model
    for that row's position) gets a null ``predicted_total_points_fpl``
    rather than one assembled from partial data.
    """
    required_columns = [
        "glm_minutes",
        *(f"glm_{component}" for component in glm_components),
        *(f"naive_{component}" for component in naive_components),
    ]
    missing = [column for column in required_columns if column not in frame.columns]
    if missing:
        raise ValueError(f"assemble_predicted_points: frame is missing column(s) {missing}")

    predicted_totals: list[float | None] = []
    for row in frame.select("season", "position", *required_columns).iter_rows(named=True):
        if any(row[column] is None for column in required_columns):
            predicted_totals.append(None)
            continue

        rules = ruleset_for_name(ruleset_name_for_season(row["season"]))
        predicted_row = PlayerFixtureRow(
            position=row["position"],
            minutes=max(0.0, row["glm_minutes"]),
            goals_scored=row["glm_goals_scored"],
            assists=row["glm_assists"],
            goals_conceded=row["glm_goals_conceded"],
            own_goals=row["naive_own_goals"],
            penalties_saved=row["naive_penalties_saved"],
            penalties_missed=row["naive_penalties_missed"],
            yellow_cards=row["naive_yellow_cards"],
            red_cards=row["naive_red_cards"],
            saves=row["naive_saves"],
            bonus=row["glm_bonus"],
            cbi=row["glm_defensive_contribution"],
            tackles=0.0,
            recoveries=0.0,
        )
        predicted_totals.append(float(rules.points(predicted_row).total))

    return frame.with_columns(
        pl.Series("predicted_total_points_fpl", predicted_totals, dtype=pl.Float64)
    )


def points_error_report(frame: pl.DataFrame) -> pl.DataFrame:
    """RMSE/MAE of ``predicted_total_points_fpl`` against
    ``label_total_points_fpl``, one row for ``"overall"`` plus one row per
    :data:`OUTCOME_BUCKETS` name - bucketed by the *realised* total (plan
    Q10), since the question is how accurate the prediction was for
    players who actually landed in each bucket."""
    paired = frame.select("label_total_points_fpl", "predicted_total_points_fpl").drop_nulls()

    rows = [
        {
            "bucket": "overall",
            **component_regression_metrics(
                paired,
                actual_column="label_total_points_fpl",
                predicted_column="predicted_total_points_fpl",
            ),
        }
    ]

    bucketed = paired.with_columns(
        pl.col("label_total_points_fpl")
        .map_elements(outcome_bucket, return_dtype=pl.Utf8)
        .alias("bucket")
    )
    for bucket in OUTCOME_BUCKETS:
        subset = bucketed.filter(pl.col("bucket") == bucket)
        rows.append(
            {
                "bucket": bucket,
                **component_regression_metrics(
                    subset,
                    actual_column="label_total_points_fpl",
                    predicted_column="predicted_total_points_fpl",
                ),
            }
        )
    return pl.DataFrame(rows)


def spearman_by_gameweek(frame: pl.DataFrame) -> pl.DataFrame:
    """One row per ``(season, event)``: Spearman rank correlation between
    ``predicted_total_points_fpl`` and ``label_total_points_fpl`` across
    that gameweek's player pool (plan Step 30) - rank correlation over the
    player-selection signal, since the model's job is picking the right
    players rather than matching each one's score exactly.

    A gameweek with fewer than 2 scored players (after dropping nulls) has
    no defined rank correlation and gets ``None`` rather than a spurious
    NaN or a divide-by-zero.
    """
    paired = frame.select(
        "season", "event", "label_total_points_fpl", "predicted_total_points_fpl"
    ).drop_nulls()

    rows = []
    for (season, event), group in paired.group_by(["season", "event"], maintain_order=True):
        correlation = None
        if group.height >= 2:
            statistic, _p_value = scipy_stats.spearmanr(
                group["label_total_points_fpl"].to_numpy(),
                group["predicted_total_points_fpl"].to_numpy(),
            )
            correlation = None if np.isnan(statistic) else float(statistic)
        rows.append(
            {"season": season, "event": event, "n_players": group.height, "spearman": correlation}
        )
    return pl.DataFrame(rows)
