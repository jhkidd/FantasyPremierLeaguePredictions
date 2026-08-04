"""Point-in-time team resolution for the feature library.

For a *target* fixture (the one being predicted for), a player's team must
be resolved without leaking any information not actually available at
``as_of``:

1. If the target fixture has already been played and a ``facts/player_fixture``
   row exists for this player at this fixture, that row's own ``team_id`` is
   used — it is already accurate for that exact match.
2. Otherwise (a genuinely future/unplayed target fixture, or a past one this
   season's facts have not been built for yet), fall back to this player's
   most recent ``facts/player_fixture`` row strictly before ``as_of`` — the
   last team we can *prove* they were registered to.
3. If neither exists (a brand-new player with no history at all), fall back
   to FPL's current ``players`` table team-of-record and count this player
   in :class:`TeamResolutionDiagnostics` — this is expected to be rare (new
   signings/promotions) and the diagnostics exist to prove it stays rare
   rather than becoming a systematic gap.

Never uses same-day-or-later data for cases 2/3 — ``as_of`` is the single
point-in-time cutoff throughout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet

__all__ = [
    "TeamResolutionDiagnostics",
    "resolve_teams",
]


@dataclass(frozen=True)
class TeamResolutionDiagnostics:
    """How team resolution went for one :func:`resolve_teams` call.

    ``fallback_to_current_team`` lists every ``player_id`` that had no
    ``facts/player_fixture`` history at all before ``as_of`` (case 3 above)
    and therefore had to use FPL's current team-of-record rather than a
    proven point-in-time value. A growing list here across many builds is a
    systemic-gap signal, not just an occasional new-signing edge case.
    """

    fallback_to_current_team: tuple[int, ...] = field(default_factory=tuple)

    @property
    def fallback_count(self) -> int:
        return len(self.fallback_to_current_team)


def _current_players(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    path = paths.staged_table("players", season, data_root=data_root) / "part.parquet"
    if not path.exists():
        return None
    return read_parquet(path).select(["player_id", "team_id", "element_type", "now_cost"])


def _player_fixture_facts(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    path = paths.facts_table("player_fixture", season, data_root=data_root) / "part.parquet"
    if not path.exists():
        return None
    frame = read_parquet(path).select(["fixture_id", "player_id", "team_id", "kickoff_time"])
    # Parquet round-trips can drop the UTC tz annotation (naive vs. aware
    # datetime64[us]); normalise to UTC-aware here so comparisons against
    # `as_of` (always tz-aware) never raise a polars SchemaError.
    if frame.schema["kickoff_time"].time_zone is None:
        frame = frame.with_columns(pl.col("kickoff_time").dt.replace_time_zone("UTC"))
    return frame


def resolve_teams(
    season: Season,
    player_ids: list[int],
    target_fixtures: pl.DataFrame,
    *,
    as_of,
    data_root: Path | None = None,
) -> tuple[dict[tuple[int, int], int | None], TeamResolutionDiagnostics]:
    """Resolve each ``(player_id, fixture_id)`` pair in ``target_fixtures`` to
    a ``team_id``.

    ``target_fixtures`` must have ``fixture_id`` and ``kickoff_time`` columns.
    Returns a mapping keyed by ``(player_id, fixture_id)`` to a ``team_id``
    (``None`` only when the player cannot be resolved at all — no current
    players-table row either), plus diagnostics for the last-resort fallback.
    """
    current_players = _current_players(season, data_root=data_root)
    current_team_by_player: dict[int, int] = {}
    if current_players is not None:
        current_team_by_player = dict(
            current_players.select("player_id", "team_id").iter_rows()
        )

    facts = _player_fixture_facts(season, data_root=data_root)

    result: dict[tuple[int, int], int | None] = {}
    fallback_players: set[int] = set()

    fixture_rows = target_fixtures.select("fixture_id", "kickoff_time").iter_rows(named=True)
    fixtures = list(fixture_rows)

    for player_id in player_ids:
        player_history = None
        if facts is not None:
            player_history = facts.filter(pl.col("player_id") == player_id)

        for fixture in fixtures:
            fixture_id = fixture["fixture_id"]
            team_id: int | None = None

            # Case 1: this exact target fixture was already played by this
            # player - use its own recorded team_id.
            if player_history is not None:
                exact = player_history.filter(pl.col("fixture_id") == fixture_id)
                if exact.height > 0:
                    team_id = exact.row(0, named=True)["team_id"]

            # Case 2: most recent facts row strictly before as_of.
            if team_id is None and player_history is not None:
                prior = player_history.filter(pl.col("kickoff_time") < as_of).sort(
                    "kickoff_time", descending=True
                )
                if prior.height > 0:
                    team_id = prior.row(0, named=True)["team_id"]

            # Case 3: last resort - current players-table team-of-record.
            if team_id is None:
                team_id = current_team_by_player.get(player_id)
                if team_id is not None:
                    fallback_players.add(player_id)

            result[(player_id, fixture_id)] = team_id

    diagnostics = TeamResolutionDiagnostics(
        fallback_to_current_team=tuple(sorted(fallback_players))
    )
    return result, diagnostics
