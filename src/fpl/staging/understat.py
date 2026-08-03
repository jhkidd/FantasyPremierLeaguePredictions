"""Stage Understat's season-aggregate and per-match payloads.

Two source endpoints, three staged tables: ``getLeagueData`` covers a whole
season in one call and yields two of them (``understat_players_season``,
one row per player's season total; ``understat_fixtures``, one row per
match with final score/xG but no player detail). ``getMatchData`` covers
one fixture per call and yields the third (``understat_player_match``, one
row per player who featured in that fixture) - this is the genuine
per-player-per-fixture grain the design needs xG/xA priors at (plan
§7.10-7.11).

Understat's own JSON has no separate season/side/match_id columns on the
roster rows that need them downstream - ``stage_match_data`` stamps those
on from what the caller already knows (which match this response came from,
and which of ``rosters.h``/``rosters.a`` a row came from), the same pattern
``staging/clubelo.py``'s ``stage_ratings`` uses for ``as_of_date``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import polars as pl

from fpl.config import Season
from fpl.staging.base import ColumnSpec, StagingReport, TableSpec, stage_frame

__all__ = [
    "FIXTURES_SPEC",
    "PLAYER_MATCH_SPEC",
    "PLAYERS_SEASON_SPEC",
    "StagedFixtures",
    "StagedPlayerMatch",
    "StagedPlayersSeason",
    "stage_fixtures",
    "stage_league_players",
    "stage_match_data",
]

PLAYERS_SEASON_SPEC = TableSpec(
    table="understat_players_season",
    key=("player_id",),
    columns=(
        ColumnSpec("player_id", "id", pl.Int64),
        ColumnSpec("player_name", "player_name", pl.Utf8),
        ColumnSpec("team_title", "team_title", pl.Utf8),
        ColumnSpec("position", "position", pl.Utf8),
        ColumnSpec("games", "games", pl.Int64),
        ColumnSpec("minutes", "time", pl.Int64),
        ColumnSpec("goals", "goals", pl.Int64),
        ColumnSpec("xg", "xG", pl.Float64),
        ColumnSpec("assists", "assists", pl.Int64),
        ColumnSpec("xa", "xA", pl.Float64),
        ColumnSpec("shots", "shots", pl.Int64),
        ColumnSpec("key_passes", "key_passes", pl.Int64),
        ColumnSpec("yellow_cards", "yellow_cards", pl.Int64),
        ColumnSpec("red_cards", "red_cards", pl.Int64),
        ColumnSpec("non_penalty_goals", "npg", pl.Int64),
        ColumnSpec("non_penalty_xg", "npxG", pl.Float64),
        ColumnSpec("xg_chain", "xGChain", pl.Float64),
        ColumnSpec("xg_buildup", "xGBuildup", pl.Float64),
    ),
)

FIXTURES_SPEC = TableSpec(
    table="understat_fixtures",
    key=("match_id",),
    columns=(
        ColumnSpec("match_id", "id", pl.Int64),
        ColumnSpec("is_result", "isResult", pl.Boolean),
        ColumnSpec("datetime", "datetime", pl.Utf8),
        ColumnSpec("home_team", "home_team", pl.Utf8),
        ColumnSpec("away_team", "away_team", pl.Utf8),
        ColumnSpec("home_goals", "home_goals", pl.Int64, required=False),
        ColumnSpec("away_goals", "away_goals", pl.Int64, required=False),
        ColumnSpec("home_xg", "home_xg", pl.Float64, required=False),
        ColumnSpec("away_xg", "away_xg", pl.Float64, required=False),
    ),
)

PLAYER_MATCH_SPEC = TableSpec(
    table="understat_player_match",
    key=("match_id", "player_id"),
    columns=(
        ColumnSpec("match_id", "match_id", pl.Int64),
        ColumnSpec("side", "side", pl.Utf8),
        ColumnSpec("player_id", "player_id", pl.Int64),
        ColumnSpec("player_name", "player", pl.Utf8),
        ColumnSpec("team_id", "team_id", pl.Int64),
        ColumnSpec("position", "position", pl.Utf8),
        ColumnSpec("minutes", "time", pl.Int64),
        ColumnSpec("goals", "goals", pl.Int64),
        ColumnSpec("own_goals", "own_goals", pl.Int64),
        ColumnSpec("shots", "shots", pl.Int64),
        ColumnSpec("xg", "xG", pl.Float64),
        ColumnSpec("assists", "assists", pl.Int64),
        ColumnSpec("xa", "xA", pl.Float64),
        ColumnSpec("key_passes", "key_passes", pl.Int64),
        ColumnSpec("yellow_card", "yellow_card", pl.Int64),
        ColumnSpec("red_card", "red_card", pl.Int64),
        ColumnSpec("xg_chain", "xGChain", pl.Float64),
        ColumnSpec("xg_buildup", "xGBuildup", pl.Float64),
    ),
)


@dataclass(frozen=True)
class StagedPlayersSeason:
    frame: pl.DataFrame
    report: StagingReport


@dataclass(frozen=True)
class StagedFixtures:
    frame: pl.DataFrame
    report: StagingReport


@dataclass(frozen=True)
class StagedPlayerMatch:
    frame: pl.DataFrame
    report: StagingReport


def _empty_frame(spec: TableSpec) -> pl.DataFrame:
    """A zero-row frame declaring every one of ``spec``'s source columns as
    ``Utf8``, so ``stage_frame``'s required-column check passes even when
    Understat published nothing at all - an empty season/match is a
    legitimate answer, not a schema violation."""
    return pl.DataFrame(schema={column.source_name: pl.Utf8 for column in spec.columns})


def stage_league_players(body: bytes, season: Season) -> StagedPlayersSeason:
    """Stage ``getLeagueData``'s ``players`` list - one row per player's
    season aggregate. Every numeric field Understat publishes here arrives
    as a string (confirmed live), so casting is left to ``stage_frame``'s
    existing ``cast(strict=False)`` rather than parsed by hand."""
    payload = json.loads(body)
    players = payload.get("players") or []
    raw = pl.DataFrame(players) if players else _empty_frame(PLAYERS_SEASON_SPEC)
    staged, report = stage_frame(raw, PLAYERS_SEASON_SPEC)
    staged = staged.with_columns(pl.lit(str(season)).alias("season")).select(
        ["season", *staged.columns]
    )
    return StagedPlayersSeason(frame=staged, report=report)


def stage_fixtures(body: bytes, season: Season) -> StagedFixtures:
    """Stage ``getLeagueData``'s ``dates`` list - one row per fixture, final
    score/xG only, no player detail (that is ``getMatchData``'s job)."""
    payload = json.loads(body)
    dates = payload.get("dates") or []
    rows = []
    for entry in dates:
        goals = entry.get("goals") or {}
        xg = entry.get("xG") or {}
        home = entry.get("h") or {}
        away = entry.get("a") or {}
        rows.append(
            {
                "id": entry.get("id"),
                "isResult": entry.get("isResult"),
                "datetime": entry.get("datetime"),
                "home_team": home.get("title"),
                "away_team": away.get("title"),
                "home_goals": goals.get("h"),
                "away_goals": goals.get("a"),
                "home_xg": xg.get("h"),
                "away_xg": xg.get("a"),
            }
        )
    raw = pl.DataFrame(rows) if rows else _empty_frame(FIXTURES_SPEC)
    staged, report = stage_frame(raw, FIXTURES_SPEC)
    staged = staged.with_columns(pl.lit(str(season)).alias("season")).select(
        ["season", *staged.columns]
    )
    return StagedFixtures(frame=staged, report=report)


def stage_match_data(body: bytes, match_id: int, season: Season) -> StagedPlayerMatch:
    """Stage one ``getMatchData`` response's ``rosters.h``/``rosters.a`` into
    one row per player who featured, with ``match_id`` and ``side`` stamped
    on from the caller - Understat's own roster rows carry neither."""
    payload = json.loads(body)
    rosters = payload.get("rosters") or {}
    rows = []
    for side in ("h", "a"):
        for player in (rosters.get(side) or {}).values():
            rows.append({**player, "match_id": match_id, "side": side})
    raw = pl.DataFrame(rows) if rows else _empty_frame(PLAYER_MATCH_SPEC)
    staged, report = stage_frame(raw, PLAYER_MATCH_SPEC)
    staged = staged.with_columns(pl.lit(str(season)).alias("season")).select(
        ["season", *staged.columns]
    )
    return StagedPlayerMatch(frame=staged, report=report)
