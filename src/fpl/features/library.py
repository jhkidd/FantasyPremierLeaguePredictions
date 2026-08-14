"""``features.build`` — the public entrypoint for the feature library.

Builds one row per ``(player_id, fixture_id)`` for every fixture in the
requested horizon (default: the single next unplayed gameweek), for every
player in the season's staged ``players`` table whose resolved team has a
fixture in that horizon. A double-gameweek team yields two rows for one
player; a blank-gameweek team yields none — both fall out naturally from
"does this player's resolved team have a fixture in this event".

This module is the only place that assembles the full feature row: it
resolves each player's team (:mod:`fpl.features.team_resolution`), builds
their rolling-window history features (:mod:`fpl.features.rolling`), joins
team-context features (:mod:`fpl.features.team_context`), attaches
position/price-at-``as_of`` (both known in advance, not leaky), and attaches
realised label columns when the target fixture has already been played
(``None`` at inference time, non-null for training-set construction) — this
one function serves both call sites, per design.

Never materialised to disk as the source of truth — this is a pure
in-memory computation; any parquet snapshot written elsewhere (the CLI's
debug output) is for inspection only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.features.rolling import (
    build_rolling_features,
)
from fpl.features.team_context import TEAM_CONTEXT_COLUMNS, build_team_context_features
from fpl.features.team_resolution import TeamResolutionDiagnostics, resolve_teams
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet

__all__ = [
    "FeaturesResult",
    "build",
]

_ELEMENT_TYPE_TO_POSITION: dict[int, str] = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

_LABEL_COLUMNS: tuple[str, ...] = ("minutes", "total_points_fpl")


@dataclass(frozen=True)
class FeaturesResult:
    frame: pl.DataFrame | None
    diagnostics: TeamResolutionDiagnostics
    detail: str = ""


def _fpl_fixtures(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    path = paths.staged_table("fixtures", season, data_root=data_root) / "part.parquet"
    if not path.exists():
        return None
    frame = read_parquet(path)
    return frame.with_columns(
        pl.col("kickoff_time").str.strptime(
            pl.Datetime(time_unit="us", time_zone="UTC"), strict=False
        )
    )


def _fpl_players(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    path = paths.staged_table("players", season, data_root=data_root) / "part.parquet"
    if not path.exists():
        return None
    return read_parquet(path).select(["player_id", "team_id", "element_type", "now_cost"])


def _player_fixture_facts(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    path = paths.facts_table("player_fixture", season, data_root=data_root) / "part.parquet"
    if not path.exists():
        return None
    frame = read_parquet(path)
    # Parquet round-trips can drop the UTC tz annotation (naive vs. aware
    # datetime64[us]); normalise to UTC-aware so comparisons against `as_of`
    # (always tz-aware) never raise a polars SchemaError.
    if frame.schema["kickoff_time"].time_zone is None:
        frame = frame.with_columns(pl.col("kickoff_time").dt.replace_time_zone("UTC"))
    return frame


def _horizon_fixtures(fixtures: pl.DataFrame, *, as_of, horizon_gameweeks: int) -> pl.DataFrame:
    """Fixtures on/after ``as_of``, restricted to the first
    ``horizon_gameweeks`` distinct ``event`` values among them."""
    upcoming = fixtures.filter(
        pl.col("kickoff_time").is_not_null() & (pl.col("kickoff_time") >= as_of)
    ).sort("kickoff_time")
    events = [e for e in upcoming["event"].unique(maintain_order=True).to_list() if e is not None]
    selected_events = set(events[:horizon_gameweeks])
    return upcoming.filter(pl.col("event").is_in(selected_events))


def _previous_season(season: Season) -> Season:
    return Season(season.start_year - 1)


def build(
    season: Season,
    as_of,
    *,
    horizon_gameweeks: int = 1,
    data_root: Path | None = None,
) -> FeaturesResult:
    """Build the feature table for one ``(season, as_of)`` request.

    ``as_of`` must be a timezone-aware ``datetime``. Returns a
    :class:`FeaturesResult` with ``frame=None`` (and a ``detail`` message)
    when the required staged/facts tables do not exist yet — a normal,
    expected state rather than an error, mirroring the ``facts/*`` modules'
    own contract.
    """
    fixtures = _fpl_fixtures(season, data_root=data_root)
    players = _fpl_players(season, data_root=data_root)
    if fixtures is None or players is None:
        return FeaturesResult(None, TeamResolutionDiagnostics(), "missing staged fixtures/players")

    facts = _player_fixture_facts(season, data_root=data_root)
    prior_facts = _player_fixture_facts(_previous_season(season), data_root=data_root)

    horizon = _horizon_fixtures(fixtures, as_of=as_of, horizon_gameweeks=horizon_gameweeks)
    if horizon.height == 0:
        return FeaturesResult(
            pl.DataFrame(schema={"player_id": pl.Int64, "fixture_id": pl.Int64}),
            TeamResolutionDiagnostics(),
            "no fixtures in horizon",
        )

    player_ids = players["player_id"].to_list()
    team_by_key, diagnostics = resolve_teams(
        season, player_ids, horizon, as_of=as_of, data_root=data_root
    )

    fixture_teams: dict[int, tuple[int, int]] = {
        row["fixture_id"]: (row["team_h"], row["team_a"]) for row in horizon.iter_rows(named=True)
    }
    fixture_meta: dict[int, dict] = {
        row["fixture_id"]: row for row in horizon.iter_rows(named=True)
    }

    player_meta = {row["player_id"]: row for row in players.iter_rows(named=True)}

    rows: list[dict] = []
    team_by_fixture_for_context: dict[tuple[int, int], int | None] = {}

    for player_id in player_ids:
        history = None
        if facts is not None:
            history = facts.filter(
                (pl.col("player_id") == player_id) & (pl.col("kickoff_time") < as_of)
            ).sort("kickoff_time")

        season_to_date_history = history

        last_season_history = None
        if prior_facts is not None:
            last_season_history = prior_facts.filter(pl.col("player_id") == player_id)

        for fixture_id, teams in fixture_teams.items():
            team_id = team_by_key.get((player_id, fixture_id))
            if team_id is None or team_id not in teams:
                continue

            team_by_fixture_for_context[(player_id, fixture_id)] = team_id

            rolling_history = history if history is not None else pl.DataFrame()
            rolling_features = build_rolling_features(
                rolling_history,
                season_to_date_history=season_to_date_history,
                last_season_history=last_season_history,
            )

            meta = player_meta.get(player_id, {})
            position = _ELEMENT_TYPE_TO_POSITION.get(meta.get("element_type"))

            fixture_row = fixture_meta[fixture_id]
            label_row = None
            if facts is not None:
                # Labels intentionally reflect the actual outcome whenever
                # it's known (regardless of as_of) - this is what makes
                # build() reusable for training-set construction: as_of
                # gates the *feature* columns only, never the label, since
                # the label is the target being predicted, not an input.
                exact = facts.filter(
                    (pl.col("player_id") == player_id) & (pl.col("fixture_id") == fixture_id)
                )
                if exact.height > 0:
                    label_row = exact.row(0, named=True)

            row = {
                "player_id": player_id,
                "fixture_id": fixture_id,
                "season": str(season),
                "event": fixture_row.get("event"),
                "team_id": team_id,
                "position": position,
                "price": meta.get("now_cost"),
                **rolling_features,
            }
            for label in _LABEL_COLUMNS:
                row[f"label_{label}"] = label_row[label] if label_row is not None else None
            rows.append(row)

    if not rows:
        empty_schema = {"player_id": pl.Int64, "fixture_id": pl.Int64}
        return FeaturesResult(pl.DataFrame(schema=empty_schema), diagnostics, "")

    frame = pl.DataFrame(rows)

    team_context = build_team_context_features(
        season, team_by_fixture_for_context, data_root=data_root
    )
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

    return FeaturesResult(frame, diagnostics, "")
