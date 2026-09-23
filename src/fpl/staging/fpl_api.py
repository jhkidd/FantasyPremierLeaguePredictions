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
    "EVENT_LIVE_PLAYER_FIXTURE_STATS_SPEC",
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
    "stage_event_live",
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
        # ``finished`` alone lags reality by up to a couple of days: bonus/BPS
        # keep shifting until FPL marks the gameweek `data_checked`. Ingesting
        # ``event_live`` before that would capture provisional numbers that
        # later move (close-live-ingestion-gap.md).
        ColumnSpec("data_checked", "data_checked", pl.Boolean, required=False),
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


# -- event_live -> player_fixture_stats (the live-ingestion gap, spec plan
# close-live-ingestion-gap.md) --

_ELEMENT_TYPE_TO_POSITION: dict[int, str] = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
"""FPL's own numeric position code. Mirrors
:data:`fpl.staging.vaastav._ELEMENT_TYPE_TO_POSITION` — duplicated rather than
imported, so this module keeps depending only on what the live API itself
provides (module docstring)."""

_EVENT_LIVE_STATS_DROP = frozenset(
    {"clean_sheets", "influence", "creativity", "threat", "ict_index", "in_dreamteam", "played"}
)
"""``event_live``'s own ``stats`` fields we deliberately never import — either
derived ourselves (``clean_sheets``, from minutes+goals_conceded) or not
modelled at all (spec §7 drops these same fields from every other FPL-API
source, `staging/vaastav.py`'s ``_DROP_COMMON``)."""

EVENT_LIVE_PLAYER_FIXTURE_STATS_SPEC = TableSpec(
    table="player_fixture_stats",
    key=("player_id", "fixture_id"),
    columns=(
        ColumnSpec("player_id", "player_id", pl.Int64),
        ColumnSpec("fixture_id", "fixture_id", pl.Int64),
        ColumnSpec("event", "event", pl.Int64),
        ColumnSpec("kickoff_time", "kickoff_time", pl.Utf8, required=False),
        ColumnSpec("was_home", "was_home", pl.Boolean, required=False),
        ColumnSpec("opponent_team", "opponent_team", pl.Int64, required=False),
        ColumnSpec("position", "position", pl.Utf8, required=False),
        ColumnSpec("player_code", "player_code", pl.Utf8, required=False),
        ColumnSpec("minutes", "minutes", pl.Int64),
        ColumnSpec("starts", "starts", pl.Int64, required=False),
        ColumnSpec("goals_scored", "goals_scored", pl.Int64),
        ColumnSpec("assists", "assists", pl.Int64),
        ColumnSpec("goals_conceded", "goals_conceded", pl.Int64),
        ColumnSpec("own_goals", "own_goals", pl.Int64),
        ColumnSpec("penalties_saved", "penalties_saved", pl.Int64),
        ColumnSpec("penalties_missed", "penalties_missed", pl.Int64),
        ColumnSpec("yellow_cards", "yellow_cards", pl.Int64),
        ColumnSpec("red_cards", "red_cards", pl.Int64),
        ColumnSpec("saves", "saves", pl.Int64),
        ColumnSpec("bonus_fpl", "bonus", pl.Int64),
        ColumnSpec("bps_fpl", "bps", pl.Int64),
        ColumnSpec("total_points_fpl", "total_points", pl.Int64),
        ColumnSpec(
            "clearances_blocks_interceptions",
            "clearances_blocks_interceptions",
            pl.Int64,
            required=False,
        ),
        ColumnSpec("tackles", "tackles", pl.Int64, required=False),
        ColumnSpec("recoveries", "recoveries", pl.Int64, required=False),
        ColumnSpec("defensive_contribution", "defensive_contribution", pl.Int64, required=False),
        ColumnSpec("expected_goals", "expected_goals", pl.Float64, required=False),
        ColumnSpec("expected_assists", "expected_assists", pl.Float64, required=False),
        ColumnSpec(
            "expected_goal_involvements", "expected_goal_involvements", pl.Float64, required=False
        ),
        ColumnSpec(
            "expected_goals_conceded", "expected_goals_conceded", pl.Float64, required=False
        ),
        # The ~15 detailed BPS-input columns (attempted_passes, key_passes,
        # etc.) are never present in event_live — FPL retired this Opta-era
        # breakdown before 2020/21 (Finding 2, `facts/player_fixture.py`).
        # Declared here, absent from the raw frame below, so they null-fill
        # exactly like every other season since, rather than needing a
        # special case.
        ColumnSpec("attempted_passes", "attempted_passes", pl.Int64, required=False),
        ColumnSpec("completed_passes", "completed_passes", pl.Int64, required=False),
        ColumnSpec("key_passes", "key_passes", pl.Int64, required=False),
        ColumnSpec("big_chances_created", "big_chances_created", pl.Int64, required=False),
        ColumnSpec("big_chances_missed", "big_chances_missed", pl.Int64, required=False),
        ColumnSpec("open_play_crosses", "open_play_crosses", pl.Int64, required=False),
        ColumnSpec("dribbles", "dribbles", pl.Int64, required=False),
        ColumnSpec("tackled", "tackled", pl.Int64, required=False),
        ColumnSpec("fouls", "fouls", pl.Int64, required=False),
        ColumnSpec("offside", "offside", pl.Int64, required=False),
        ColumnSpec("target_missed", "target_missed", pl.Int64, required=False),
        ColumnSpec("errors_leading_to_goal", "errors_leading_to_goal", pl.Int64, required=False),
        ColumnSpec(
            "errors_leading_to_goal_attempt",
            "errors_leading_to_goal_attempt",
            pl.Int64,
            required=False,
        ),
        ColumnSpec("penalties_conceded", "penalties_conceded", pl.Int64, required=False),
        ColumnSpec("winning_goals", "winning_goals", pl.Int64, required=False),
    ),
    drop=_EVENT_LIVE_STATS_DROP,
)


def stage_event_live(
    body: bytes,
    season: Season,
    event: int,
    *,
    players: pl.DataFrame,
    fixtures: pl.DataFrame,
) -> tuple[pl.DataFrame, StagingReport]:
    """Stage one gameweek's ``event/{event}/live/`` capture into
    ``player_fixture_stats`` rows for the current, in-progress season.

    Matches ``stage_merged_gw``'s E7 output shape column-for-column, so
    ``facts/player_fixture.py`` needs no source-specific branch — the two
    only ever populate disjoint seasons.

    ``players`` and ``fixtures`` must already be staged for ``season`` (an
    empty frame yields an all-null ``opponent_team``/``was_home``/
    ``position``, not a failure — mirrors ``_with_team_codes``'s "absent
    input -> null, log, don't fail" convention; the caller is expected to log
    that condition itself, since it has the season/table context to say so).

    A player with more than one fixture this gameweek (a "double gameweek")
    is skipped and counted in the report's ``excluded`` dict: ``event_live``'s
    per-player ``stats`` block is a gameweek total, not split by fixture, so a
    double-gameweek row cannot be attributed to one fixture without guessing
    — left absent rather than guessed at (mirrors
    ``_derive_team_id_from_fixture``'s "ambiguous -> refuse" stance).
    """
    payload: dict[str, Any] = json.loads(body)
    elements: list[dict[str, Any]] = payload.get("elements", [])

    rows: list[dict[str, Any]] = []
    double_gameweek = 0
    for element in elements:
        explain = element.get("explain") or []
        fixture_ids = [item["fixture"] for item in explain if item.get("fixture") is not None]
        if not fixture_ids:
            continue  # a blank gameweek for this player: no fixture to attach the row to
        if len(fixture_ids) > 1:
            double_gameweek += 1
            continue
        row = dict(element.get("stats") or {})
        row["player_id"] = element["id"]
        row["fixture_id"] = fixture_ids[0]
        rows.append(row)

    if not rows:
        report = StagingReport(
            "player_fixture_stats",
            len(elements),
            0,
            (),
            excluded={"double_gameweek": double_gameweek},
        )
        return pl.DataFrame(), report

    raw = pl.DataFrame(rows).with_columns(pl.lit(event).alias("event"))

    player_lookup = (
        players.select("player_id", "code", "team_id", "element_type")
        .with_columns(pl.col("code").cast(pl.Utf8).alias("player_code"))
        .drop("code")
    )
    raw = raw.join(player_lookup, on="player_id", how="left")

    fixture_lookup = fixtures.select("fixture_id", "team_h", "team_a", "kickoff_time")
    raw = raw.join(fixture_lookup, on="fixture_id", how="left")

    raw = raw.with_columns(
        pl.when(pl.col("team_id") == pl.col("team_h"))
        .then(True)
        .when(pl.col("team_id") == pl.col("team_a"))
        .then(False)
        .otherwise(None)
        .alias("was_home"),
        pl.when(pl.col("team_id") == pl.col("team_h"))
        .then(pl.col("team_a"))
        .when(pl.col("team_id") == pl.col("team_a"))
        .then(pl.col("team_h"))
        .otherwise(None)
        .cast(pl.Int64)
        .alias("opponent_team"),
        pl.col("element_type")
        .replace_strict(_ELEMENT_TYPE_TO_POSITION, default=None, return_dtype=pl.Utf8)
        .alias("position"),
    ).drop(["team_id", "element_type", "team_h", "team_a"])

    staged, report = stage_frame(raw, EVENT_LIVE_PLAYER_FIXTURE_STATS_SPEC)
    staged = staged.with_columns(pl.lit(str(season)).alias("season")).select(
        ["season", *staged.columns]
    )
    report = StagingReport(
        report.table,
        report.rows_in,
        report.rows_out,
        report.unknown_columns,
        excluded={"double_gameweek": double_gameweek},
    )
    return staged, report
