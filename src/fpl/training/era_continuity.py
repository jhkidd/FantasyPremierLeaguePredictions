"""Defensive-contribution era-continuity experiment (plan Phase A Step 32,
Q8/A8).

FPL's raw ``defensive_contribution`` field is null for every season before
2025-26 (the season the corresponding scoring rule debuted), *except* it is
also null for 2019-20 through 2024-25 while its own raw components -
``cbi``/``tackles``/``recoveries`` - are non-null for exactly three earlier
seasons, 2016-17 through 2018-19 (confirmed empirically against real data;
matches plan §0.2's ``obs_defensive`` availability table). So there is a
genuine, if narrow, window of pre-2025-26 seasons where a defensive
contribution *count* is reconstructible even though FPL never labelled it
one at the time.

``quality.checks._defensive_contribution_formula_gate`` already validates
that ``cbi + tackles`` (DEF) / ``cbi + tackles + recoveries`` (MID/FWD)
reproduces the real ``defensive_contribution`` value wherever both happen
to be observed together (2025-26). This module reuses that same formula to
derive a training-only label for 2016-17..2018-19, purely to make the
plan's Q8/A8 experiment possible: fit the DC GLM component on that derived
label, then evaluate it against the *real* ``label_defensive_contribution``
in the 2025-26 test split - the plan's one sanctioned, one-time exception
to the "never touch test" boundary every other evaluation function in this
package observes, since DC has no real label anywhere in the validation
split (2024-25) to check against instead.

This derivation is scoped to this one experiment only. It never writes
back to ``facts/player_fixture`` or the training matrix on disk, and every
other consumer of ``label_defensive_contribution`` continues to see it
null for 2016-17..2018-19, exactly as the real data is.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet
from fpl.training.baseline import (
    fit_glm_baseline,
    naive_rolling_mean_predictions,
    predict_glm_baseline,
)
from fpl.training.evaluation import component_regression_metrics
from fpl.training.splits import TEST_SEASON

__all__ = [
    "DC_ERA_TRAIN_SEASONS",
    "defensive_contribution_era_continuity_report",
]

# The only three seasons where FPL's raw cbi/tackles/recoveries columns are
# populated before the 2025-26 rule reintroduced defensive_contribution
# itself (plan §0.2's obs_defensive availability table).
DC_ERA_TRAIN_SEASONS: tuple[str, ...] = ("2016-17", "2017-18", "2018-19")

# The DC formula gate was only ever verified for outfield positions -
# goalkeepers are excluded from the derived label entirely, matching that
# same scope (and DC scoring never applies to GK regardless).
_DC_FORMULA_POSITIONS: tuple[str, ...] = ("DEF", "MID", "FWD")

_DERIVED_LABEL_SCHEMA: dict[str, pl.DataType] = {
    "season": pl.Utf8,
    "fixture_id": pl.Int64,
    "player_id": pl.Int64,
    "label_defensive_contribution": pl.Float64,
}


def _derived_defensive_contribution_labels(
    seasons: tuple[str, ...], *, data_root: Path | None = None
) -> pl.DataFrame:
    """One row per ``(season, fixture_id, player_id)`` across ``seasons``
    with a derived ``label_defensive_contribution`` computed straight from
    ``facts/player_fixture``'s raw ``cbi``/``tackles``/``recoveries``,
    using the same position-dependent formula
    ``quality.checks._defensive_contribution_formula_gate`` validates
    wherever the real field happens to be observed too. A season whose
    facts have not been built yet is silently skipped, matching every
    other facts-reading function in this package."""
    frames: list[pl.DataFrame] = []
    for season_str in seasons:
        season = Season.parse(season_str)
        path = paths.facts_table("player_fixture", season, data_root=data_root) / "part.parquet"
        if not path.exists():
            continue
        facts = read_parquet(path)
        frames.append(
            facts.select(
                "season", "fixture_id", "player_id", "position", "cbi", "tackles", "recoveries"
            )
        )
    if not frames:
        return pl.DataFrame(schema=_DERIVED_LABEL_SCHEMA)

    combined = pl.concat(frames, how="vertical").filter(
        pl.col("position").is_in(_DC_FORMULA_POSITIONS)
        & pl.col("cbi").is_not_null()
        & pl.col("tackles").is_not_null()
        & pl.col("recoveries").is_not_null()
    )
    combined = combined.with_columns(
        pl.when(pl.col("position") == "DEF")
        .then(pl.col("cbi") + pl.col("tackles"))
        .otherwise(pl.col("cbi") + pl.col("tackles") + pl.col("recoveries"))
        .cast(pl.Float64)
        .alias("label_defensive_contribution")
    )
    return combined.select(list(_DERIVED_LABEL_SCHEMA))


def defensive_contribution_era_continuity_report(
    full_frame: pl.DataFrame, *, data_root: Path | None = None
) -> pl.DataFrame:
    """Fit the DC GLM component on 2016-17..2018-19 (its derived label),
    evaluate on the real 2025-26 test-split label - plan Step 32's
    dedicated era-continuity subsection.

    Returns one row per ``("overall", "DEF", "MID", "FWD")`` group (GK is
    out of scope - see module docstring) for each of the GLM and a
    same-season naive trailing-mean baseline (no era assumption at all,
    fit fresh within 2025-26's own real history), with MAE/RMSE/Poisson
    deviance against the real ``label_defensive_contribution`` in 2025-26.
    A group with no fitted model or no real label to compare against
    reports ``n=0`` rather than raising, matching
    :func:`fpl.training.evaluation.component_regression_metrics`'s own
    null-safe contract.
    """
    era_train = full_frame.filter(pl.col("season").is_in(DC_ERA_TRAIN_SEASONS)).drop(
        "label_defensive_contribution"
    )
    derived_labels = _derived_defensive_contribution_labels(
        DC_ERA_TRAIN_SEASONS, data_root=data_root
    )
    era_train = era_train.join(
        derived_labels, on=["season", "fixture_id", "player_id"], how="inner"
    )

    bundle = fit_glm_baseline(era_train, components=("defensive_contribution",))

    test_era = full_frame.filter(pl.col("season") == TEST_SEASON)
    test_with_glm = predict_glm_baseline(bundle, test_era, components=("defensive_contribution",))
    test_with_predictions = naive_rolling_mean_predictions(
        test_with_glm, targets=["defensive_contribution"]
    )

    groups: list[tuple[str, pl.DataFrame]] = [("overall", test_with_predictions)]
    for position in _DC_FORMULA_POSITIONS:
        groups.append((position, test_with_predictions.filter(pl.col("position") == position)))

    rows: list[dict[str, float | int | str | None]] = []
    for label, group_frame in groups:
        for model_name, predicted_column in (
            ("glm", "glm_defensive_contribution"),
            ("naive", "naive_defensive_contribution"),
        ):
            metrics = component_regression_metrics(
                group_frame,
                actual_column="label_defensive_contribution",
                predicted_column=predicted_column,
                poisson=True,
            )
            rows.append({"group": label, "model": model_name, **metrics})

    return pl.DataFrame(rows)
