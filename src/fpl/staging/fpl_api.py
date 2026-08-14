"""Stage the FPL API's bootstrap-static and fixtures payloads.

Spec §6: raw JSON in, one typed table per concept out. This module only
touches what phases 1-3 already capture — the multi-tarball vaastav era
staging lives in :mod:`fpl.staging.vaastav`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import polars as pl

from fpl.config import Season
from fpl.staging.base import ColumnSpec, StagingReport, TableSpec, stage_frame

__all__ = [
    "AVAILABILITY_SNAPSHOTS_SPEC",
    "ENTRY_SNAPSHOTS_SPEC",
    "EVENTS_SPEC",
    "FIXTURES_SPEC",
    "MANAGER_PICKS_SPEC",
    "PLAYERS_SPEC",
    "PRICE_SNAPSHOTS_SPEC",
    "TEAMS_SPEC",
    "StagedBootstrap",
    "stage_availability_snapshots",
    "stage_bootstrap_static",
    "stage_entry_snapshots",
    "stage_fixtures",
    "stage_manager_picks",
    "stage_price_snapshots",
]


PLAYERS_SPEC = TableSpec(
    table="players",
    key=("player_id",),
    columns=(
        ColumnSpec("player_id", "id", pl.Int64),
        ColumnSpec("code", "code", pl.Int64),
        ColumnSpec("team_id", "team", pl.Int64),
        ColumnSpec("element_type", "element_type", pl.Int64),
        ColumnSpec("first_name", "first_name", pl.Utf8),
        ColumnSpec("second_name", "second_name", pl.Utf8),
        ColumnSpec("web_name", "web_name", pl.Utf8),
        ColumnSpec("status", "status", pl.Utf8),
        ColumnSpec("now_cost", "now_cost", pl.Int64),
        ColumnSpec("selected_by_percent", "selected_by_percent", pl.Float64),
        ColumnSpec("news", "news", pl.Utf8, required=False),
        ColumnSpec(
            "chance_of_playing_next_round", "chance_of_playing_next_round", pl.Int64, required=False
        ),
        ColumnSpec("total_points", "total_points", pl.Int64),
        ColumnSpec("minutes", "minutes", pl.Int64),
        ColumnSpec("goals_scored", "goals_scored", pl.Int64),
        ColumnSpec("assists", "assists", pl.Int64),
        ColumnSpec("clean_sheets", "clean_sheets", pl.Int64),
        ColumnSpec("goals_conceded", "goals_conceded", pl.Int64),
        ColumnSpec("own_goals", "own_goals", pl.Int64),
        ColumnSpec("penalties_saved", "penalties_saved", pl.Int64),
        ColumnSpec("penalties_missed", "penalties_missed", pl.Int64),
        ColumnSpec("yellow_cards", "yellow_cards", pl.Int64),
        ColumnSpec("red_cards", "red_cards", pl.Int64),
        ColumnSpec("saves", "saves", pl.Int64),
        ColumnSpec("bonus", "bonus", pl.Int64),
        ColumnSpec("bps", "bps", pl.Int64),
        ColumnSpec("starts", "starts", pl.Int64, required=False, group="expected"),
        ColumnSpec(
            "expected_goals", "expected_goals", pl.Float64, required=False, group="expected"
        ),
        ColumnSpec(
            "expected_assists", "expected_assists", pl.Float64, required=False, group="expected"
        ),
        ColumnSpec(
            "expected_goal_involvements",
            "expected_goal_involvements",
            pl.Float64,
            required=False,
            group="expected",
        ),
        ColumnSpec(
            "expected_goals_conceded",
            "expected_goals_conceded",
            pl.Float64,
            required=False,
            group="expected",
        ),
        ColumnSpec(
            "clearances_blocks_interceptions",
            "clearances_blocks_interceptions",
            pl.Int64,
            required=False,
            group="defensive",
        ),
        ColumnSpec("tackles", "tackles", pl.Int64, required=False, group="defensive"),
        ColumnSpec("recoveries", "recoveries", pl.Int64, required=False, group="defensive"),
        ColumnSpec(
            "defensive_contribution",
            "defensive_contribution",
            pl.Int64,
            required=False,
            group="defensive",
        ),
    ),
    drop=frozenset({"ep_next", "ep_this", "form", "value_form", "value_season"}),
)

TEAMS_SPEC = TableSpec(
    table="teams",
    key=("team_id",),
    columns=(
        ColumnSpec("team_id", "id", pl.Int64),
        ColumnSpec("code", "code", pl.Int64),
        ColumnSpec("name", "name", pl.Utf8),
        ColumnSpec("short_name", "short_name", pl.Utf8),
        ColumnSpec("strength", "strength", pl.Int64, required=False),
    ),
    drop=frozenset({"form"}),
)

EVENTS_SPEC = TableSpec(
    table="events",
    key=("event",),
    columns=(
        ColumnSpec("event", "id", pl.Int64),
        ColumnSpec("name", "name", pl.Utf8),
        ColumnSpec("deadline_time", "deadline_time", pl.Utf8),
        ColumnSpec("finished", "finished", pl.Boolean),
        ColumnSpec("is_current", "is_current", pl.Boolean),
        ColumnSpec("is_next", "is_next", pl.Boolean),
        ColumnSpec("is_previous", "is_previous", pl.Boolean),
        ColumnSpec("average_entry_score", "average_entry_score", pl.Int64, required=False),
    ),
)

FIXTURES_SPEC = TableSpec(
    table="fixtures",
    key=("fixture_id",),
    columns=(
        ColumnSpec("fixture_id", "id", pl.Int64),
        ColumnSpec("code", "code", pl.Int64),
        ColumnSpec("event", "event", pl.Int64, required=False),
        ColumnSpec("kickoff_time", "kickoff_time", pl.Utf8, required=False),
        ColumnSpec("team_h", "team_h", pl.Int64),
        ColumnSpec("team_a", "team_a", pl.Int64),
        ColumnSpec("team_h_score", "team_h_score", pl.Int64, required=False),
        ColumnSpec("team_a_score", "team_a_score", pl.Int64, required=False),
        ColumnSpec("finished", "finished", pl.Boolean),
        ColumnSpec("minutes", "minutes", pl.Int64, required=False),
    ),
    drop=frozenset({"stats"}),
)


@dataclass(frozen=True)
class StagedBootstrap:
    players: pl.DataFrame
    teams: pl.DataFrame
    events: pl.DataFrame
    reports: tuple[StagingReport, ...]


def _with_season(frame: pl.DataFrame, season: Season) -> pl.DataFrame:
    return frame.with_columns(pl.lit(str(season)).alias("season")).select(
        ["season", *frame.columns]
    )


def stage_bootstrap_static(body: bytes, season: Season) -> StagedBootstrap:
    """Stage the ``elements``/``teams``/``events`` lists of one bootstrap-static capture."""
    payload: dict[str, Any] = json.loads(body)

    players_raw = pl.DataFrame(payload["elements"])
    teams_raw = pl.DataFrame(payload["teams"])
    events_raw = pl.DataFrame(payload["events"])

    players, players_report = stage_frame(players_raw, PLAYERS_SPEC)
    teams, teams_report = stage_frame(teams_raw, TEAMS_SPEC)
    events, events_report = stage_frame(events_raw, EVENTS_SPEC)

    return StagedBootstrap(
        players=_with_season(players, season),
        teams=_with_season(teams, season),
        events=_with_season(events, season),
        reports=(players_report, teams_report, events_report),
    )


def stage_fixtures(body: bytes, season: Season) -> tuple[pl.DataFrame, StagingReport]:
    payload: list[dict[str, Any]] = json.loads(body)
    raw = pl.DataFrame(payload)
    staged, report = stage_frame(raw, FIXTURES_SPEC)
    return _with_season(staged, season), report


# -- snapshot tables, built from several as_of captures of bootstrap-static --

PRICE_SNAPSHOTS_SPEC = TableSpec(
    table="price_snapshots",
    key=("player_id",),
    columns=(
        ColumnSpec("player_id", "id", pl.Int64),
        ColumnSpec("now_cost", "now_cost", pl.Int64),
        ColumnSpec("cost_change_event", "cost_change_event", pl.Int64, required=False),
        ColumnSpec("selected_by_percent", "selected_by_percent", pl.Float64),
        ColumnSpec("transfers_in_event", "transfers_in_event", pl.Int64, required=False),
        ColumnSpec("transfers_out_event", "transfers_out_event", pl.Int64, required=False),
    ),
)

AVAILABILITY_SNAPSHOTS_SPEC = TableSpec(
    table="availability_snapshots",
    key=("player_id",),
    columns=(
        ColumnSpec("player_id", "id", pl.Int64),
        ColumnSpec("status", "status", pl.Utf8),
        ColumnSpec("news", "news", pl.Utf8, required=False),
        ColumnSpec(
            "chance_of_playing_next_round", "chance_of_playing_next_round", pl.Int64, required=False
        ),
    ),
)


def _stage_bootstrap_snapshot(
    body: bytes, spec: TableSpec, season: Season, as_of: datetime
) -> tuple[pl.DataFrame, StagingReport]:
    payload: dict[str, Any] = json.loads(body)
    raw = pl.DataFrame(payload["elements"])
    staged, report = stage_frame(raw, spec)
    staged = staged.with_columns(
        pl.lit(str(season)).alias("season"),
        pl.lit(as_of.isoformat()).alias("as_of_ts"),
    ).select(["season", "as_of_ts", *staged.columns])
    return staged, report


def stage_price_snapshots(
    captures: list[tuple[bytes, datetime]], season: Season
) -> tuple[pl.DataFrame, list[StagingReport]]:
    """Fold every historical bootstrap-static capture into one snapshot table.

    ``as_of_ts`` is the true capture time, not an approximation — unlike
    vaastav's per-gameweek market fields, the live API is captured on a real
    clock (spec plan §4.1 decisions table).
    """
    frames: list[pl.DataFrame] = []
    reports: list[StagingReport] = []
    for body, as_of in captures:
        staged, report = _stage_bootstrap_snapshot(body, PRICE_SNAPSHOTS_SPEC, season, as_of)
        frames.append(staged)
        reports.append(report)
    combined = pl.concat(frames) if frames else pl.DataFrame()
    return combined, reports


def stage_availability_snapshots(
    captures: list[tuple[bytes, datetime]], season: Season
) -> tuple[pl.DataFrame, list[StagingReport]]:
    frames: list[pl.DataFrame] = []
    reports: list[StagingReport] = []
    for body, as_of in captures:
        staged, report = _stage_bootstrap_snapshot(body, AVAILABILITY_SNAPSHOTS_SPEC, season, as_of)
        frames.append(staged)
        reports.append(report)
    combined = pl.concat(frames) if frames else pl.DataFrame()
    return combined, reports


# -- entry / manager-picks tables --

ENTRY_SNAPSHOTS_SPEC = TableSpec(
    table="entry_snapshots",
    key=("entry_id",),
    columns=(
        ColumnSpec("entry_id", "id", pl.Int64),
        ColumnSpec("summary_overall_points", "summary_overall_points", pl.Int64, required=False),
        ColumnSpec("summary_overall_rank", "summary_overall_rank", pl.Int64, required=False),
        ColumnSpec("summary_event_points", "summary_event_points", pl.Int64, required=False),
        ColumnSpec("last_deadline_bank", "last_deadline_bank", pl.Int64, required=False),
        ColumnSpec("last_deadline_value", "last_deadline_value", pl.Int64, required=False),
        ColumnSpec(
            "last_deadline_total_transfers",
            "last_deadline_total_transfers",
            pl.Int64,
            required=False,
        ),
    ),
)


def stage_entry_snapshots(
    captures: list[tuple[bytes, datetime]], season: Season
) -> tuple[pl.DataFrame, list[StagingReport]]:
    """One row per ``entry`` capture — the manager's own profile over time."""
    frames: list[pl.DataFrame] = []
    reports: list[StagingReport] = []
    for body, as_of in captures:
        payload: dict[str, Any] = json.loads(body)
        raw = pl.DataFrame([payload])
        staged, report = stage_frame(raw, ENTRY_SNAPSHOTS_SPEC)
        staged = staged.with_columns(
            pl.lit(str(season)).alias("season"),
            pl.lit(as_of.isoformat()).alias("as_of_ts"),
        ).select(["season", "as_of_ts", *staged.columns])
        frames.append(staged)
        reports.append(report)
    combined = pl.concat(frames) if frames else pl.DataFrame()
    return combined, reports


MANAGER_PICKS_SPEC = TableSpec(
    table="manager_picks",
    key=("event", "entry_id", "player_id"),
    columns=(
        ColumnSpec("entry_id", "entry", pl.Int64),
        ColumnSpec("event", "event", pl.Int64),
        ColumnSpec("player_id", "element", pl.Int64),
        ColumnSpec("position", "position", pl.Int64, required=False),
        ColumnSpec("multiplier", "multiplier", pl.Int64, required=False),
        ColumnSpec("is_captain", "is_captain", pl.Boolean, required=False),
        ColumnSpec("is_vice_captain", "is_vice_captain", pl.Boolean, required=False),
    ),
)


def stage_manager_picks(
    records: list[dict[str, Any]], season: Season, cohort: str
) -> tuple[pl.DataFrame, StagingReport]:
    """Stage one cohort's ``entry_picks`` ndjson records into flat pick rows.

    ``contaminated`` — automatic substitutions were applied before capture —
    is a property of the whole squad-at-a-gameweek, not of an individual pick,
    so it is carried through onto every row that squad contributes rather than
    computed downstream where the link back to the raw flag could be lost.

    A cohort is staged alone and stamped with its own name; cohorts are never
    concatenated here; pooling them is a modelling decision made later, never
    a staging default (spec §6.1 — the elite and mini populations must never
    be pooled).
    """
    rows: list[dict[str, Any]] = []
    for record in records:
        entry = record["entry"]
        event = record["event"]
        contaminated = bool(record.get("contaminated"))
        for pick in record["payload"]["picks"]:
            row = dict(pick)
            row["entry"] = entry
            row["event"] = event
            row["contaminated"] = contaminated
            rows.append(row)

    raw = pl.DataFrame(rows)
    staged, report = stage_frame(raw, MANAGER_PICKS_SPEC)
    staged = staged.with_columns(
        pl.lit(str(season)).alias("season"),
        pl.lit(cohort).alias("cohort"),
        pl.Series("contaminated", [r["contaminated"] for r in rows]),
    ).select(["season", "cohort", *staged.columns, "contaminated"])
    return staged, report
