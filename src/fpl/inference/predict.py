"""``predict_next_gameweek`` — the production inference entrypoint (Phase C
step 4, `.github/context/subsystem3-close-and-mvp-site.md`).

Loads the frozen model registry (:mod:`fpl.training.registry`), builds the
feature frame the same way training does
(:func:`fpl.features.library.build` — already documented there as the one
function both call sites share), and reuses the existing GLM/naive/
assembly machinery unchanged rather than duplicating any of it:

- :func:`~fpl.training.baseline.predict_glm_baseline` for the ``glm_*``
  component predictions.
- :func:`~fpl.inference.naive.predict_naive_components` for the six
  ``naive_*`` components GLM never models.
- :func:`~fpl.training.evaluation.assemble_predicted_points` to combine
  every predicted component into ``predicted_total_points_fpl`` through
  that row's own season's scoring ruleset.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.features import library as features_library
from fpl.inference.naive import predict_naive_components
from fpl.training.baseline import predict_glm_baseline
from fpl.training.evaluation import assemble_predicted_points
from fpl.training.registry import load_active_bundle

__all__ = ["predict_next_gameweek"]

_EMPTY_SCHEMA: dict[str, pl.DataType] = {"player_id": pl.Int64, "fixture_id": pl.Int64}


def predict_next_gameweek(
    season: Season,
    as_of: datetime,
    *,
    horizon_gameweeks: int = 1,
    data_root: Path | None = None,
    models_root: Path | None = None,
) -> pl.DataFrame:
    """Predict every component plus ``predicted_total_points_fpl`` for
    every ``(player, fixture)`` row in the ``horizon_gameweeks`` ahead of
    ``as_of``.

    Raises whatever :func:`~fpl.training.registry.load_active_bundle`
    raises if ``models/active.json`` is missing or incomplete — there is
    nothing useful to predict with no active model, so this is a hard
    failure rather than a silent empty result.

    Raises ``ValueError`` if the built feature frame is missing any column
    the active model's ``feature_columns`` requires (spec §3.5's hard-fail
    contract — a real feature-schema drift between training and inference
    must be caught here, not fed to the model as an all-null column).

    Returns an empty frame (mirroring
    :func:`~fpl.features.library.build`'s own "missing is normal" contract)
    when there is nothing to predict — no staged players/fixtures yet, or
    no fixtures fall in the requested horizon.
    """
    bundle = load_active_bundle(models_root=models_root)

    result = features_library.build(
        season, as_of, horizon_gameweeks=horizon_gameweeks, data_root=data_root
    )
    if result.frame is None or result.frame.height == 0:
        return pl.DataFrame(schema=_EMPTY_SCHEMA)

    missing_features = [
        column for column in bundle.feature_columns if column not in result.frame.columns
    ]
    if missing_features:
        raise ValueError(
            "predict_next_gameweek: the built feature frame is missing required "
            f"feature column(s) the active model expects: {missing_features}"
        )

    predicted = predict_glm_baseline(bundle, result.frame)

    player_ids = predicted["player_id"].unique().to_list()
    naive = predict_naive_components(season, as_of, player_ids, data_root=data_root)
    predicted = predicted.join(naive, on="player_id", how="left")

    return assemble_predicted_points(predicted)
