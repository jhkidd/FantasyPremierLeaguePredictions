"""Team-context feature join: ``facts/team_fixture`` -> per-player features.

Team-context features are a simple per-fixture join, never an aggregation:
each ``(player_id, fixture_id)`` row picks up its resolved team's own
``facts/team_fixture`` row for that exact fixture, unmodified. All
aggregation (rolling windows, masking) belongs to ``features/rolling.py``;
this module's only job is "look up the row for this team at this fixture".

A ``(team_id, fixture_id)`` pair with no matching ``facts/team_fixture`` row
(e.g. team-fixture facts not yet built for a newly-added fixture) yields
nulls for every team-context column rather than a missing row — mirroring
``facts/player_fixture``'s "null columns, never a dropped row" discipline.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.facts.team_fixture import CONGESTION_WINDOWS
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet

__all__ = [
    "TEAM_CONTEXT_COLUMNS",
    "build_team_context_features",
]

TEAM_CONTEXT_COLUMNS: tuple[str, ...] = (
    "elo_rating",
    "opponent_elo_rating",
    *(f"fixture_count_prior_{w}_days" for w in CONGESTION_WINDOWS),
    "odds_implied_win_prob",
    "odds_implied_draw_prob",
    "odds_implied_loss_prob",
)


def _team_fixture_facts(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    path = paths.facts_table("team_fixture", season, data_root=data_root) / "part.parquet"
    if not path.exists():
        return None
    return read_parquet(path).select(["fixture_id", "team_id", *TEAM_CONTEXT_COLUMNS])


def build_team_context_features(
    season: Season,
    team_by_fixture: dict[tuple[int, int], int | None],
    *,
    data_root: Path | None = None,
) -> dict[tuple[int, int], dict[str, float | None]]:
    """Look up team-context features for every ``(player_id, fixture_id)``
    key in ``team_by_fixture``, whose values are the resolved ``team_id``
    for that pair (as produced by ``features.team_resolution.resolve_teams``).

    Returns a mapping from the same ``(player_id, fixture_id)`` key to a
    dict of team-context feature values, all null when the team could not
    be resolved or has no matching ``facts/team_fixture`` row for that
    fixture.
    """
    facts = _team_fixture_facts(season, data_root=data_root)
    lookup: dict[tuple[int, int], dict[str, float | None]] = {}
    if facts is not None:
        for row in facts.iter_rows(named=True):
            lookup[(row["team_id"], row["fixture_id"])] = {
                column: row[column] for column in TEAM_CONTEXT_COLUMNS
            }

    empty = {column: None for column in TEAM_CONTEXT_COLUMNS}
    result: dict[tuple[int, int], dict[str, float | None]] = {}
    for (player_id, fixture_id), team_id in team_by_fixture.items():
        if team_id is None:
            result[(player_id, fixture_id)] = dict(empty)
            continue
        result[(player_id, fixture_id)] = dict(lookup.get((team_id, fixture_id), empty))
    return result
