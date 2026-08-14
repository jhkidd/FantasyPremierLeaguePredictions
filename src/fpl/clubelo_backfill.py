"""Resumable multi-date Club Elo backfill (plan §0.6, Phase 0 Step 14).

Elo is a *point-in-time* rating: the number for Arsenal on 2018-03-10 is not
recoverable from today's endpoint, so a decade of ratings can only be had by
asking for a decade of dates, one request each. Club Elo serves historical
dates correctly (verified live across 2016 → 2026, every response carrying
``From``/``To`` windows that bracket the queried date).

The date list is derived from the fixtures we actually need to rate rather
than from a calendar range: ratings for days no Premier League match was
played would be fetched and stored for nothing. That comes to ~1,153 distinct
dates across ten seasons instead of ~3,600 calendar days.

**Why resumability is explicit here.** ``write_raw`` already skips a write
whose content hashes identically to the latest partition, but that is a
*content* check against one partition, not a date check across all of them —
resuming a half-finished run would re-fetch every date, and at ~7s per request
that is over two wasted hours. So this module reads ``params.date`` out of
every existing partition's ``meta.json`` first and only fetches what is
genuinely missing.

The T-1 offset (rate a fixture using the rating published the day *before*
kickoff) is applied here, at the call site, exactly as
:mod:`fpl.sources.clubelo` documents: Elo updates same-day once a match is
played, so querying a fixture's own date risks reading a rating that already
reflects that day's result — leakage, and invisible in the data.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.log import get_logger
from fpl.sources.clubelo import ClubEloConnector
from fpl.sources.errors import SourceError
from fpl.storage import paths
from fpl.storage.raw_io import META_FILENAME, RawArtifact, write_raw

__all__ = [
    "BackfillOutcome",
    "backfill_clubelo_ratings",
    "captured_dates",
    "rating_dates_for_season",
]

logger = get_logger(__name__)

RATING_OFFSET = timedelta(days=1)
"""Fetch the rating published the day *before* kickoff (plan §7.2)."""


@dataclass(frozen=True)
class BackfillOutcome:
    """What one backfill run did, per season and in total."""

    season: Season
    dates_in_scope: int
    fetched: int
    skipped: int
    failed: tuple[tuple[date, str], ...] = field(default=())

    @property
    def complete(self) -> bool:
        return not self.failed and self.fetched + self.skipped == self.dates_in_scope


def rating_dates_for_season(season: Season, *, data_root: Path | None = None) -> list[date]:
    """The distinct dates whose ratings this season's fixtures need.

    Derived from ``facts/player_fixture`` rather than a calendar range: only
    days a match was actually played need a rating, and only one request is
    needed per date however many fixtures fall on it.

    Returns an empty list when the facts table has not been built — that is a
    normal ordering state (facts before backfill), not an error.
    """
    part = paths.facts_table("player_fixture", season, data_root=data_root) / "part.parquet"
    if not part.is_file():
        return []
    frame = pl.read_parquet(part, columns=["fixture_id", "kickoff_time"])
    kickoffs = (
        frame.filter(pl.col("kickoff_time").is_not_null())
        .select(pl.col("kickoff_time").dt.date().alias("kickoff_date"))
        .unique()
        .to_series()
        .to_list()
    )
    return sorted({kickoff - RATING_OFFSET for kickoff in kickoffs})


def captured_dates(season: Season, *, data_root: Path | None = None) -> set[date]:
    """Every rating date already on disk for a season.

    Reads ``params.date`` from each partition's ``meta.json``. A partition
    predating that field, or one with an unparseable date, is ignored rather
    than trusted — the cost of re-fetching one date is seconds, while wrongly
    treating a date as captured leaves a permanent hole in the history.
    """
    found: set[date] = set()
    for partition in paths.iter_as_of_partitions(
        "clubelo", "ratings", season, data_root=data_root
    ):
        meta_path = partition / META_FILENAME
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        recorded = (meta.get("params") or {}).get("date")
        if recorded is None:
            continue
        try:
            found.add(date.fromisoformat(str(recorded)))
        except (TypeError, ValueError):
            continue
    return found


def _write_capture(artifact: RawArtifact, *, data_root: Path | None) -> None:
    """Store one date's capture, working around two ``write_raw`` behaviours
    that are right for polling a live endpoint and wrong for a backfill.

    First, ``write_raw`` skips a write whose bytes hash identically to the
    latest partition. For a live poll that correctly avoids recording "the
    source was unchanged"; for a backfill it is destructive — Club Elo returns
    byte-identical ratings for consecutive days whenever no match was played
    between them, so the second date would be dropped, its ``params.date``
    never recorded, and it would be re-fetched on every subsequent resume
    without ever being stored. Hence ``force=True``: each rating date is a
    distinct observation regardless of whether its bytes repeat.

    Second, partition directories are named to one-second resolution. Requests
    are normally seconds apart, but a cached or unusually fast response can
    land two dates in the same second, where the later would silently
    overwrite the earlier. Advancing to the next free second keeps one
    partition per date, which is the invariant resumability depends on.
    """
    moment = artifact.fetched_at
    while True:
        partition = paths.raw_partition(
            artifact.source,
            artifact.endpoint,
            artifact.season,
            moment,
            data_root=data_root,
        )
        if not partition.exists():
            break
        moment = moment + timedelta(seconds=1)
    write_raw(replace(artifact, fetched_at=moment), force=True, data_root=data_root)


def backfill_clubelo_ratings(
    seasons: Sequence[Season],
    *,
    connector: ClubEloConnector | None = None,
    data_root: Path | None = None,
    limit: int | None = None,
    progress: Callable[[Season, date, int, int], None] | None = None,
) -> list[BackfillOutcome]:
    """Fetch every missing Club Elo rating date for ``seasons``.

    Safe to re-run and safe to interrupt: each date is written as its own
    partition the moment it arrives, so an interrupted run loses at most the
    request in flight, and a re-run picks up exactly where it stopped.

    A failed date is recorded and the run continues. Over a thousand sequential
    requests against a free public API, one transient failure aborting the
    whole two-hour run would be a poor trade — and the outcome names every
    failure, so a follow-up run retries precisely those dates.

    ``limit`` caps the number of *fetches* (not dates in scope), which is what
    makes a short live smoke test possible without committing to the full run.
    """
    owns_connector = connector is None
    active = connector or ClubEloConnector()
    outcomes: list[BackfillOutcome] = []
    remaining = limit
    try:
        for season in seasons:
            outcomes.append(
                _backfill_one_season(
                    season,
                    connector=active,
                    data_root=data_root,
                    remaining=remaining,
                    progress=progress,
                )
            )
            if remaining is not None:
                remaining -= outcomes[-1].fetched
                if remaining <= 0:
                    break
    finally:
        if owns_connector:
            active.close()
    return outcomes


def _backfill_one_season(
    season: Season,
    *,
    connector: ClubEloConnector,
    data_root: Path | None,
    remaining: int | None,
    progress: Callable[[Season, date, int, int], None] | None,
) -> BackfillOutcome:
    wanted = rating_dates_for_season(season, data_root=data_root)
    already = captured_dates(season, data_root=data_root)
    missing = [day for day in wanted if day not in already]
    skipped = len(wanted) - len(missing)

    fetched = 0
    failures: list[tuple[date, str]] = []
    for index, day in enumerate(missing, start=1):
        if remaining is not None and fetched >= remaining:
            break
        if progress is not None:
            progress(season, day, index, len(missing))
        try:
            body = connector.fetch_ratings(day)
        except SourceError as exc:
            failures.append((day, str(exc)))
            logger.warning("clubelo backfill failed for %s %s: %s", season, day, exc)
            continue
        artifact = connector.artifact_for_ratings(body, day, season)
        _write_capture(artifact, data_root=data_root)
        fetched += 1

    return BackfillOutcome(
        season=season,
        dates_in_scope=len(wanted),
        fetched=fetched,
        skipped=skipped,
        failed=tuple(failures),
    )


def total_dates_in_scope(
    seasons: Iterable[Season], *, data_root: Path | None = None
) -> dict[Season, int]:
    """How many rating dates each season needs — a dry-run cost estimate."""
    return {
        season: len(rating_dates_for_season(season, data_root=data_root)) for season in seasons
    }
