"""Training-matrix assembly: one row per historical player-fixture, with
features computed as-of that gameweek's deadline and labels always populated
from the realised outcome (spec §21, Phase A).

Unlike :func:`fpl.features.library.build` (the inference entrypoint), this
module never resolves a player's team via
:mod:`fpl.features.team_resolution` and never iterates a season's staged
``players`` roster. Every row it emits already has a played fixture in
``facts/player_fixture``, so its team, fixture, and label values are all
directly on that row - team resolution's "is this player's team still
uncertain" machinery has nothing to do here, and going through it would risk
emitting rows beyond facts's own row count (a roster player resolved to a
fixture they have no facts row for).

Instead, each player's own facts rows are grouped by ``event`` and walked in
gameweek order: a gameweek's rows always see only strictly-earlier gameweeks'
rows as history, and a gameweek's own rows (including a double gameweek's
second fixture) never see each other. This falls out of
:func:`fpl.training.deadlines.gameweek_deadlines`'s own no-overlap
invariant - once it has confirmed no two gameweeks' kickoff windows overlap,
sorting a player's rows by ``kickoff_time`` puts every gameweek's rows in one
contiguous block in event order, so a running "history so far" list built up
one gameweek at a time is sufficient with no extra per-row date filtering.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.features.rolling import build_rolling_features
from fpl.features.team_context import TEAM_CONTEXT_COLUMNS, build_team_context_features
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet
from fpl.training.deadlines import gameweek_deadlines

__all__ = ["IDENTITY_COLUMNS", "LABEL_COLUMNS", "OBS_COLUMNS", "build_training_matrix"]

IDENTITY_COLUMNS: tuple[str, ...] = (
    "season",
    "event",
    "fixture_id",
    "player_id",
    "player_code",
    "position",
    "was_home",
    "team_code",
    "opponent_team_code",
)

OBS_COLUMNS: tuple[str, ...] = ("obs_defensive", "obs_bps_inputs", "obs_expected", "obs_starts")

# label name -> source column on facts/player_fixture. Identical for every
# label except `bonus` (facts stores it `bonus_fpl`, mirroring
# `total_points_fpl`'s own `_fpl` suffix convention) and `total_points_fpl`
# itself (already suffixed, so no rename).
_LABEL_SOURCE_COLUMNS: dict[str, str] = {
    "minutes": "minutes",
    "goals_scored": "goals_scored",
    "assists": "assists",
    "goals_conceded": "goals_conceded",
    "bonus": "bonus_fpl",
    "defensive_contribution": "defensive_contribution",
    "saves": "saves",
    "yellow_cards": "yellow_cards",
    "red_cards": "red_cards",
    "penalties_saved": "penalties_saved",
    "penalties_missed": "penalties_missed",
    "own_goals": "own_goals",
    "total_points_fpl": "total_points_fpl",
}

LABEL_COLUMNS: tuple[str, ...] = tuple(f"label_{name}" for name in _LABEL_SOURCE_COLUMNS)


def _previous_season(season: Season) -> Season:
    return Season(season.start_year - 1)


def _player_fixture_facts(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    path = paths.facts_table("player_fixture", season, data_root=data_root) / "part.parquet"
    if not path.exists():
        return None
    frame = read_parquet(path)
    # Parquet round-trips can drop the UTC tz annotation; normalise so every
    # comparison and sort below is against a consistently tz-aware column.
    if frame.schema["kickoff_time"].time_zone is None:
        frame = frame.with_columns(pl.col("kickoff_time").dt.replace_time_zone("UTC"))
    return frame


def _empty_matrix() -> pl.DataFrame:
    schema = {
        **{column: pl.Utf8 for column in IDENTITY_COLUMNS},
        **{column: pl.Boolean for column in OBS_COLUMNS},
        **{column: pl.Float64 for column in LABEL_COLUMNS},
    }
    return pl.DataFrame(schema=schema)


def _build_one_season(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    # Raises FileNotFoundError if facts/player_fixture is missing for this
    # season, and ValueError on overlapping gameweeks - both propagate as-is,
    # since there is nothing this function can safely do instead.
    deadlines = gameweek_deadlines(season, data_root=data_root)

    facts = _player_fixture_facts(season, data_root=data_root)
    if facts is None or facts.height == 0:
        return None
    prior_facts = _player_fixture_facts(_previous_season(season), data_root=data_root)

    rows: list[dict] = []
    team_by_fixture: dict[tuple[int, int], int | None] = {}

    for (player_id,), player_facts in facts.sort("kickoff_time").group_by(
        "player_id", maintain_order=True
    ):
        last_season_history = None
        if prior_facts is not None:
            last_season_history = prior_facts.filter(pl.col("player_id") == player_id)

        # Same-gameweek rows are contiguous in kickoff-time order (guaranteed
        # by gameweek_deadlines's own no-overlap check), so history for row i
        # is simply every row strictly before its own gameweek's first row.
        history_so_far = player_facts.clear()
        for event_frame in player_facts.partition_by("event", maintain_order=True):
            event = event_frame["event"][0]
            as_of = deadlines.get(event)

            rolling_features = build_rolling_features(
                history_so_far,
                season_to_date_history=history_so_far,
                last_season_history=last_season_history,
            )

            for row in event_frame.iter_rows(named=True):
                team_by_fixture[(row["player_id"], row["fixture_id"])] = row["team_id"]

                assembled: dict = {column: row[column] for column in IDENTITY_COLUMNS}
                assembled["as_of"] = as_of
                for column in OBS_COLUMNS:
                    assembled[column] = row[column]
                assembled.update(rolling_features)
                for label, source in _LABEL_SOURCE_COLUMNS.items():
                    assembled[f"label_{label}"] = row[source]
                rows.append(assembled)

            history_so_far = pl.concat([history_so_far, event_frame], how="vertical")

    if not rows:
        return _empty_matrix()

    frame = pl.DataFrame(rows)

    team_context = build_team_context_features(season, team_by_fixture, data_root=data_root)
    context_rows = [
        {"player_id": pid, "fixture_id": fid, **features}
        for (pid, fid), features in team_context.items()
    ]
    context_frame = pl.DataFrame(
        context_rows,
        schema={
            "player_id": pl.Int64,
            "fixture_id": pl.Int64,
            **{column: pl.Float64 for column in TEAM_CONTEXT_COLUMNS},
        },
    )
    frame = frame.join(context_frame, on=["player_id", "fixture_id"], how="left")

    return frame.drop("as_of")


def build_training_matrix(
    seasons: Sequence[Season], *, data_root: Path | None = None
) -> pl.DataFrame:
    """Build the training matrix for every ``season`` in ``seasons``.

    One row per ``(season, event, player_id, fixture_id)`` played fixture,
    with rolling/team-context features computed only from strictly-earlier
    gameweeks (never the row's own gameweek or a later one), and label
    columns always populated from the realised outcome (never gated by
    ``as_of`` - the label is the prediction target, not an input).

    Raises ``FileNotFoundError`` if any requested season's
    ``facts/player_fixture`` has not been built yet, and ``ValueError`` if any
    season has overlapping gameweek kickoff windows - both surfaced directly
    from :func:`fpl.training.deadlines.gameweek_deadlines`.
    """
    frames = [_build_one_season(season, data_root=data_root) for season in seasons]
    frames = [frame for frame in frames if frame is not None]
    if not frames:
        return _empty_matrix()
    return pl.concat(frames, how="diagonal_relaxed")
