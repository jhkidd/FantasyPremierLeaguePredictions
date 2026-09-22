"""Trailing-mean predictions for the naive-only components at inference
time — the horizon-call-shaped counterpart of
:func:`fpl.training.baseline.naive_rolling_mean_predictions` for the six
targets GLM never models (:data:`NAIVE_ONLY_COMPONENTS`, also
:data:`fpl.training.evaluation.NAIVE_ONLY_COMPONENTS` — the same tuple,
re-exported here rather than duplicated, since
:func:`~fpl.training.evaluation.assemble_predicted_points` requires exactly
these ``naive_<component>`` columns).

:func:`naive_rolling_mean_predictions` operates on a fully assembled
training frame spanning many gameweeks per player, computing the trailing
window incrementally as it walks forward through history. At inference
time there is only ever one horizon call per player (the next unplayed
gameweek(s)), so the equivalent computation is simpler: for each requested
player, take their own realised fixture history strictly before ``as_of``
this season, keep the most recent ``window`` fixtures, and average each
requested component over any non-null values — identical semantics
(including the per-season reset - a player's first row of a season always
gets ``None``, since each season's ``facts/player_fixture`` table only
ever contains that season's own rows) to the training-time computation for
that same player's next unplayed fixture.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet
from fpl.training.baseline import DEFAULT_NAIVE_WINDOW
from fpl.training.evaluation import NAIVE_ONLY_COMPONENTS

__all__ = ["NAIVE_ONLY_COMPONENTS", "predict_naive_components"]


def _player_fixture_facts(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    path = paths.facts_table("player_fixture", season, data_root=data_root) / "part.parquet"
    if not path.exists():
        return None
    frame = read_parquet(path)
    # Mirrors fpl.features.library's own tz-normalisation: a parquet
    # round-trip can drop the UTC tz annotation (naive vs. aware
    # datetime64[us]), which would otherwise raise a polars SchemaError
    # comparing against `as_of` (always tz-aware).
    if frame.schema["kickoff_time"].time_zone is None:
        frame = frame.with_columns(pl.col("kickoff_time").dt.replace_time_zone("UTC"))
    return frame


def predict_naive_components(
    season: Season,
    as_of: datetime,
    player_ids: Sequence[int],
    *,
    window: int = DEFAULT_NAIVE_WINDOW,
    components: Sequence[str] = NAIVE_ONLY_COMPONENTS,
    data_root: Path | None = None,
) -> pl.DataFrame:
    """One row per ``player_id`` in ``player_ids``: a ``naive_<component>``
    column per name in ``components``, each the unweighted mean of that
    player's own last ``window`` realised fixtures strictly before
    ``as_of`` this season (module docstring). ``None`` when the player has
    no such history yet (no ``facts/player_fixture`` table for ``season``
    at all, or no fixture of theirs before ``as_of``).
    """
    schema = {
        "player_id": pl.Int64,
        **{f"naive_{component}": pl.Float64 for component in components},
    }
    if not player_ids:
        return pl.DataFrame(schema=schema)

    facts = _player_fixture_facts(season, data_root=data_root)

    rows: list[dict[str, float | int | None]] = []
    for player_id in player_ids:
        row: dict[str, float | int | None] = {"player_id": player_id}
        history = (
            facts.filter(
                (pl.col("player_id") == player_id) & (pl.col("kickoff_time") < as_of)
            ).sort("kickoff_time")
            if facts is not None
            else None
        )
        tail = history.tail(window) if history is not None else None
        for component in components:
            values = (
                [v for v in tail[component].to_list() if v is not None] if tail is not None else []
            )
            row[f"naive_{component}"] = (sum(values) / len(values)) if values else None
        rows.append(row)

    return pl.DataFrame(rows, schema=schema)
