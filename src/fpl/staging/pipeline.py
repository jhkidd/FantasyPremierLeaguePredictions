"""Orchestrates staging for the ``fpl`` source: raw partitions -> parquet tables.

Thin by design — all interpretation lives in :mod:`fpl.staging.fpl_api`. This
module only knows how to find raw partitions on disk and where staged tables
belong (spec §4/§6).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import polars as pl

from fpl.config import Config, Season
from fpl.sources.openfootball import SEASON_FILES as _OPENFOOTBALL_ENDPOINTS
from fpl.staging.base import StagingReport
from fpl.staging.clubelo import stage_ratings
from fpl.staging.footballdata import stage_matches_and_odds
from fpl.staging.fpl_api import (
    stage_availability_snapshots,
    stage_bootstrap_static,
    stage_entry_snapshots,
    stage_fixtures,
    stage_manager_picks,
    stage_price_snapshots,
)
from fpl.staging.openfootball import stage_fixtures as stage_openfootball_fixtures
from fpl.staging.understat import stage_fixtures as stage_understat_fixtures
from fpl.staging.understat import stage_league_players as stage_understat_league_players
from fpl.staging.understat import stage_match_data as stage_understat_match_data
from fpl.staging.vaastav import stage_merged_gw
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet
from fpl.storage.raw_io import partition_as_of, read_raw

__all__ = [
    "StageResult",
    "stage_clubelo_source",
    "stage_footballdata_source",
    "stage_fpl_source",
    "stage_openfootball_source",
    "stage_understat_source",
    "stage_vaastav_source",
]

_COHORTS = ("self", "mini", "elite")


@dataclass(frozen=True)
class StageResult:
    table: str
    written: bool
    rows: int
    report: StagingReport | None
    detail: str = ""


def _write(
    frame: pl.DataFrame,
    table: str,
    season: Season,
    key: tuple[str, ...],
    *,
    data_root: Path | None = None,
    filename: str = "part.parquet",
) -> Path:
    directory = paths.staged_table(table, season, data_root=data_root)
    directory.mkdir(parents=True, exist_ok=True)
    out_path = directory / filename
    write_parquet(frame, out_path, sort_by=key)
    return out_path


def _stage_bootstrap_derived(
    season: Season, data_root: Path | None, tables: set[str] | None
) -> list[StageResult]:
    partition = paths.latest_partition("fpl", "bootstrap_static", season, data_root=data_root)
    if partition is None:
        return [
            StageResult("players", False, 0, None, "no bootstrap-static capture on disk"),
        ]
    body, _meta = read_raw(partition)
    staged = stage_bootstrap_static(body, season)

    results: list[StageResult] = []
    for table_name, frame, key, report in (
        ("players", staged.players, ("player_id",), staged.reports[0]),
        ("teams", staged.teams, ("team_id",), staged.reports[1]),
        ("events", staged.events, ("event",), staged.reports[2]),
    ):
        if tables is not None and table_name not in tables:
            continue
        _write(frame, table_name, season, key, data_root=data_root)
        results.append(StageResult(table_name, True, frame.height, report))
    return results


def _stage_fixtures(
    season: Season, data_root: Path | None, tables: set[str] | None
) -> list[StageResult]:
    if tables is not None and "fixtures" not in tables:
        return []
    partition = paths.latest_partition("fpl", "fixtures", season, data_root=data_root)
    if partition is None:
        return [StageResult("fixtures", False, 0, None, "no fixtures capture on disk")]
    body, _meta = read_raw(partition)
    staged, report = stage_fixtures(body, season)
    _write(staged, "fixtures", season, ("fixture_id",), data_root=data_root)
    return [StageResult("fixtures", True, staged.height, report)]


def _bootstrap_snapshot_captures(
    season: Season, data_root: Path | None
) -> list[tuple[bytes, datetime]]:
    captures = []
    for partition in paths.iter_as_of_partitions(
        "fpl", "bootstrap_static", season, data_root=data_root
    ):
        body, _meta = read_raw(partition)
        captures.append((body, partition_as_of(partition)))
    return captures


def _stage_snapshots(
    season: Season, data_root: Path | None, tables: set[str] | None
) -> list[StageResult]:
    results: list[StageResult] = []
    wanted = {"price_snapshots", "availability_snapshots"}
    if tables is not None:
        wanted &= tables
    if not wanted:
        return results

    captures = _bootstrap_snapshot_captures(season, data_root)
    if not captures:
        for name in wanted:
            results.append(StageResult(name, False, 0, None, "no bootstrap-static capture on disk"))
        return results

    if "price_snapshots" in wanted:
        frame, reports = stage_price_snapshots(captures, season)
        if frame.height:
            _write(frame, "price_snapshots", season, ("player_id", "as_of_ts"), data_root=data_root)
        results.append(StageResult("price_snapshots", frame.height > 0, frame.height, reports[0]))

    if "availability_snapshots" in wanted:
        frame, reports = stage_availability_snapshots(captures, season)
        if frame.height:
            _write(
                frame,
                "availability_snapshots",
                season,
                ("player_id", "as_of_ts"),
                data_root=data_root,
            )
        results.append(
            StageResult("availability_snapshots", frame.height > 0, frame.height, reports[0])
        )
    return results


def _stage_entry_snapshots(
    season: Season, data_root: Path | None, tables: set[str] | None
) -> list[StageResult]:
    if tables is not None and "entry_snapshots" not in tables:
        return []
    entry_id = Config.load().entry_id
    if entry_id is None:
        return [StageResult("entry_snapshots", False, 0, None, "no team configured")]

    captures = []
    for partition in paths.iter_as_of_partitions("fpl", "entry", season, data_root=data_root):
        body, _meta = read_raw(partition)
        captures.append((body, partition_as_of(partition)))
    if not captures:
        return [StageResult("entry_snapshots", False, 0, None, "no entry capture on disk")]

    frame, reports = stage_entry_snapshots(captures, season)
    if frame.height:
        _write(frame, "entry_snapshots", season, ("entry_id", "as_of_ts"), data_root=data_root)
    return [StageResult("entry_snapshots", frame.height > 0, frame.height, reports[0])]


def _stage_manager_picks(
    season: Season, data_root: Path | None, tables: set[str] | None
) -> list[StageResult]:
    if tables is not None and "manager_picks" not in tables:
        return []

    results: list[StageResult] = []
    for cohort in _COHORTS:
        parent = paths.raw_endpoint_dir(
            "fpl", "entry_picks", season, cohort=cohort, data_root=data_root
        )
        if not parent.is_dir():
            continue
        event_dirs = sorted(
            p for p in parent.iterdir() if p.is_dir() and p.name.startswith("event=")
        )
        cohort_records: list[dict] = []
        for event_dir in event_dirs:
            event = int(event_dir.name.removeprefix("event="))
            for _index, chunk_dir in paths.iter_chunks(
                "fpl", "entry_picks", season, cohort=cohort, event=event, data_root=data_root
            ):
                body, _meta = read_raw(chunk_dir)
                for line in body.decode("utf-8").splitlines():
                    if line.strip():
                        cohort_records.append(json.loads(line))
        if not cohort_records:
            continue
        frame, report = stage_manager_picks(cohort_records, season, cohort)
        _write(
            frame,
            "manager_picks",
            season,
            ("event", "entry_id", "player_id"),
            data_root=data_root,
            filename=f"cohort={cohort}.parquet",
        )
        results.append(StageResult(f"manager_picks[{cohort}]", True, frame.height, report))
    return results


def stage_fpl_source(
    season: Season,
    *,
    data_root: Path | None = None,
    tables: set[str] | None = None,
) -> list[StageResult]:
    """Stage every FPL API table currently capturable, from what is already on disk.

    ``tables`` restricts to a subset by staged-table name. Rebuilds from raw
    every time, so an unchanged rebuild produces an empty Git diff (spec §4).
    """
    results: list[StageResult] = []
    results += _stage_bootstrap_derived(season, data_root, tables)
    results += _stage_fixtures(season, data_root, tables)
    results += _stage_snapshots(season, data_root, tables)
    results += _stage_entry_snapshots(season, data_root, tables)
    results += _stage_manager_picks(season, data_root, tables)
    return results


def stage_vaastav_source(
    season: Season,
    *,
    data_root: Path | None = None,
) -> list[StageResult]:
    """Stage vaastav's ``merged_gw.csv`` for one season into ``player_fixture_stats``.

    A season absent from :data:`fpl.staging.vaastav.ERA_BY_SEASON` raises
    rather than silently doing nothing (a new upstream season needs a person
    to classify its schema first). When that season's ``players_raw.csv``
    has also been ingested, it is passed through too — required for the
    three earliest eras (position/team derivation) and used for the stable
    ``player_code`` field in every other era.
    """
    partition = paths.latest_partition("vaastav", "merged_gw", season, data_root=data_root)
    if partition is None:
        return [
            StageResult(
                "player_fixture_stats", False, 0, None, "no vaastav merged_gw capture on disk"
            )
        ]
    body, _meta = read_raw(partition)

    players_raw_body = None
    players_raw_partition = paths.latest_partition(
        "vaastav", "players_raw", season, data_root=data_root
    )
    if players_raw_partition is not None:
        players_raw_body, _meta = read_raw(players_raw_partition)

    staged = stage_merged_gw(body, season, players_raw_body=players_raw_body)
    _write(
        staged.frame,
        "player_fixture_stats",
        season,
        ("player_id", "fixture_id"),
        data_root=data_root,
    )
    detail_parts = []
    if staged.excluded_manager_rows:
        detail_parts.append(f"excluded {staged.excluded_manager_rows} manager-asset row(s)")
    if staged.duplicate_rows_dropped:
        detail_parts.append(f"dropped {staged.duplicate_rows_dropped} exact-duplicate row(s)")
    if staged.postponed_fixture_placeholders_dropped:
        detail_parts.append(
            f"dropped {staged.postponed_fixture_placeholders_dropped} "
            "postponed-fixture placeholder row(s)"
        )
    detail = "; ".join(detail_parts)
    return [StageResult("player_fixture_stats", True, staged.frame.height, staged.report, detail)]


def stage_clubelo_source(
    season: Season,
    *,
    data_root: Path | None = None,
) -> list[StageResult]:
    """Stage every Club Elo ``ratings`` capture on disk for one season.

    Unlike the FPL price/availability snapshots this mirrors the shape of,
    each capture is its own ``as_of=`` partition rather than needing a
    combined history frame passed in one call — every captured day is staged
    independently and concatenated, so a rebuild from raw stays idempotent.
    """
    captures = list(paths.iter_as_of_partitions("clubelo", "ratings", season, data_root=data_root))
    if not captures:
        return [
            StageResult("clubelo_ratings", False, 0, None, "no clubelo ratings capture on disk")
        ]

    frames = []
    report: StagingReport | None = None
    for partition in captures:
        body, _meta = read_raw(partition)
        as_of_date = partition_as_of(partition).date()
        staged = stage_ratings(body, as_of_date, season)
        frames.append(staged.frame)
        report = staged.report
    combined = pl.concat(frames).unique(subset=["as_of_date", "club"], keep="last")
    _write(combined, "clubelo_ratings", season, ("as_of_date", "club"), data_root=data_root)
    return [StageResult("clubelo_ratings", True, combined.height, report)]


def stage_footballdata_source(
    season: Season,
    *,
    data_root: Path | None = None,
) -> list[StageResult]:
    """Stage football-data.co.uk's one match-and-odds CSV for one season."""
    partition = paths.latest_partition(
        "footballdata", "matches_and_odds", season, data_root=data_root
    )
    if partition is None:
        return [
            StageResult(
                "footballdata_matches_and_odds",
                False,
                0,
                None,
                "no footballdata matches_and_odds capture on disk",
            )
        ]
    body, _meta = read_raw(partition)
    staged = stage_matches_and_odds(body, season)
    _write(
        staged.frame,
        "footballdata_matches_and_odds",
        season,
        ("match_date", "home_team", "away_team"),
        data_root=data_root,
    )
    return [StageResult("footballdata_matches_and_odds", True, staged.frame.height, staged.report)]


def stage_openfootball_source(
    season: Season,
    *,
    data_root: Path | None = None,
) -> list[StageResult]:
    """Stage every `openfootball/champions-league` file captured for one season.

    A competition file that was never captured (e.g. no tracked club reached
    the Conference League qualifying rounds that season) is a legitimate,
    silent absence, not a defect (mirrors ``sources/openfootball.py``'s own
    per-file optionality) — each of :data:`_OPENFOOTBALL_ENDPOINTS`'s
    endpoints is staged independently.
    """
    results: list[StageResult] = []
    for endpoint in _OPENFOOTBALL_ENDPOINTS.values():
        partition = paths.latest_partition("openfootball", endpoint, season, data_root=data_root)
        if partition is None:
            continue
        body, _meta = read_raw(partition)
        staged = stage_openfootball_fixtures(body, season, endpoint)
        _write(
            staged.frame,
            "openfootball_fixtures",
            season,
            ("competition", "match_date", "home_team", "away_team"),
            data_root=data_root,
            filename=f"competition={endpoint}.parquet",
        )
        results.append(
            StageResult(
                f"openfootball_fixtures[{endpoint}]", True, staged.frame.height, staged.report
            )
        )
    if not results:
        return [
            StageResult("openfootball_fixtures", False, 0, None, "no openfootball capture on disk")
        ]
    return results


def stage_understat_source(
    season: Season,
    *,
    data_root: Path | None = None,
) -> list[StageResult]:
    """Stage Understat's season-aggregate and per-match captures for one
    season into three tables (plan §7.10-7.11): ``understat_players_season``
    and ``understat_fixtures`` both come from one ``getLeagueData`` capture,
    ``understat_player_match`` comes from however many ``getMatchData``
    chunks have been captured so far - a partial per-match backfill still
    stages whatever chunks exist, rather than requiring the whole season's
    sweep to finish first.
    """
    results: list[StageResult] = []

    league_partition = paths.latest_partition(
        "understat", "league_data", season, data_root=data_root
    )
    if league_partition is None:
        return [
            StageResult(
                "understat_players_season",
                False,
                0,
                None,
                "no understat league_data capture on disk",
            ),
            StageResult(
                "understat_fixtures", False, 0, None, "no understat league_data capture on disk"
            ),
        ]

    body, _meta = read_raw(league_partition)
    players_staged = stage_understat_league_players(body, season)
    _write(
        players_staged.frame,
        "understat_players_season",
        season,
        ("player_id",),
        data_root=data_root,
    )
    results.append(
        StageResult(
            "understat_players_season", True, players_staged.frame.height, players_staged.report
        )
    )

    fixtures_staged = stage_understat_fixtures(body, season)
    _write(fixtures_staged.frame, "understat_fixtures", season, ("match_id",), data_root=data_root)
    results.append(
        StageResult(
            "understat_fixtures", True, fixtures_staged.frame.height, fixtures_staged.report
        )
    )

    match_frames = []
    report: StagingReport | None = None
    for _index, chunk_dir in paths.iter_chunks(
        "understat", "match_data", season, data_root=data_root
    ):
        chunk_body, _meta = read_raw(chunk_dir)
        for line in chunk_body.decode("utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            staged = stage_understat_match_data(
                json.dumps(record["payload"]).encode("utf-8"), record["match_id"], season
            )
            match_frames.append(staged.frame)
            report = staged.report

    if match_frames:
        combined = pl.concat(match_frames).unique(subset=["match_id", "player_id"], keep="last")
        _write(
            combined,
            "understat_player_match",
            season,
            ("match_id", "player_id"),
            data_root=data_root,
        )
        results.append(StageResult("understat_player_match", True, combined.height, report))
    else:
        results.append(
            StageResult(
                "understat_player_match", False, 0, None, "no understat match_data chunk on disk"
            )
        )

    return results
