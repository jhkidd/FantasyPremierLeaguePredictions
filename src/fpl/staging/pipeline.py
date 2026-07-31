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
from fpl.staging.base import StagingReport
from fpl.staging.fpl_api import (
    stage_availability_snapshots,
    stage_bootstrap_static,
    stage_entry_snapshots,
    stage_fixtures,
    stage_manager_picks,
    stage_price_snapshots,
)
from fpl.staging.vaastav import stage_merged_gw
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet
from fpl.storage.raw_io import partition_as_of, read_raw

__all__ = ["StageResult", "stage_fpl_source", "stage_vaastav_source"]

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

    Only the seasons classified in :data:`fpl.staging.vaastav.ERA_BY_SEASON`
    can be staged — phase 4 classifies 2025/26 alone (the resequencing
    decision), so an earlier season raises rather than silently doing
    nothing.
    """
    partition = paths.latest_partition("vaastav", "merged_gw", season, data_root=data_root)
    if partition is None:
        return [
            StageResult(
                "player_fixture_stats", False, 0, None, "no vaastav merged_gw capture on disk"
            )
        ]
    body, _meta = read_raw(partition)
    staged = stage_merged_gw(body, season)
    _write(
        staged.frame,
        "player_fixture_stats",
        season,
        ("player_id", "fixture_id"),
        data_root=data_root,
    )
    detail = (
        f"excluded {staged.excluded_manager_rows} manager-asset row(s)"
        if staged.excluded_manager_rows
        else ""
    )
    return [StageResult("player_fixture_stats", True, staged.frame.height, staged.report, detail)]
