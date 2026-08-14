"""Tests for the resumable Club Elo backfill (plan §0.6, Phase 0 Step 14).

Two properties matter more than anything else here and are tested hardest:

1. **Resumability is date-level, not content-level.** ``write_raw`` skips a
   write whose bytes match the latest partition, which is *not* the same as
   knowing a date was already fetched. Relying on it would re-request all
   1,153 dates on every resume — over two hours of wasted requests against a
   free public API — so the skip is an explicit pre-fetch check.

2. **The T-1 offset is applied.** Club Elo updates a club's rating the same
   day it plays. Asking for a fixture's own date can therefore return a
   rating that already reflects that fixture's result: target leakage that
   would inflate validation scores and be invisible in the stored data.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from fpl.clubelo_backfill import (
    backfill_clubelo_ratings,
    captured_dates,
    rating_dates_for_season,
    total_dates_in_scope,
)
from fpl.config import Season
from fpl.sources.errors import SourceError
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet

SEASON = Season(2025)

RATINGS_CSV = (
    b"Rank,Club,Country,Level,Elo,From,To\n"
    b"1,Arsenal,ENG,1,2063.7578125,2025-05-31,2025-08-21\n"
)


class FakeConnector:
    """Records what was asked for, so tests can assert on the request list.

    Deliberately not a mock: what these tests care about is the exact set of
    dates requested, which is the thing a mock's call list expresses most
    awkwardly and the thing most likely to regress.
    """

    VERSION = "1"
    SOURCE = "clubelo"

    def __init__(self, *, fail_on: set[date] | None = None) -> None:
        self.requested: list[date] = []
        self.fail_on = fail_on or set()
        self.closed = False
        self.base_url = "http://api.clubelo.com"

    def fetch_ratings(self, as_of_date: date) -> bytes:
        self.requested.append(as_of_date)
        if as_of_date in self.fail_on:
            raise SourceError(f"boom on {as_of_date}")
        return RATINGS_CSV.replace(b"Arsenal", f"Club{as_of_date.day}".encode())

    def artifact_for_ratings(self, body: bytes, as_of_date: date, season: Season):
        from fpl.storage.raw_io import RawArtifact

        return RawArtifact(
            source="clubelo",
            endpoint="ratings",
            season=season,
            url=f"{self.base_url}/{as_of_date.isoformat()}",
            http_status=200,
            body=body,
            fetched_at=datetime.now(UTC),
            connector_version="1",
            params={"date": as_of_date.isoformat()},
            content_type="csv",
        )

    def close(self) -> None:
        self.closed = True


def _write_facts(data_root: Path, kickoffs: list[str], season: Season = SEASON) -> None:
    """A minimal ``player_fixture`` table — only the two columns the date
    derivation reads."""
    out_dir = paths.facts_table("player_fixture", season, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        {
            "fixture_id": list(range(1, len(kickoffs) + 1)),
            "kickoff_time": kickoffs,
        }
    ).with_columns(
        pl.col("kickoff_time").str.strptime(pl.Datetime(time_unit="us", time_zone="UTC"))
    )
    write_parquet(frame, out_dir / "part.parquet")


class TestRatingDatesForSeason:
    def test_uses_the_day_before_kickoff(self, tmp_path: Path) -> None:
        """Elo updates same-day after a match, so a fixture's own date can
        already reflect its own result (plan §7.2)."""
        data_root = tmp_path / "data"
        _write_facts(data_root, ["2025-08-16T14:00:00Z"])

        assert rating_dates_for_season(SEASON, data_root=data_root) == [date(2025, 8, 15)]

    def test_deduplicates_fixtures_sharing_a_matchday(self, tmp_path: Path) -> None:
        """Ten fixtures on a Saturday need one request, not ten — the whole
        reason the run is two hours rather than a day."""
        data_root = tmp_path / "data"
        _write_facts(data_root, ["2025-08-16T12:30:00Z"] * 3 + ["2025-08-16T17:30:00Z"])

        assert rating_dates_for_season(SEASON, data_root=data_root) == [date(2025, 8, 15)]

    def test_dates_are_sorted(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            ["2025-12-26T15:00:00Z", "2025-08-16T14:00:00Z", "2025-10-04T14:00:00Z"],
        )

        assert rating_dates_for_season(SEASON, data_root=data_root) == [
            date(2025, 8, 15),
            date(2025, 10, 3),
            date(2025, 12, 25),
        ]

    def test_null_kickoffs_are_ignored(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        out_dir = paths.facts_table("player_fixture", SEASON, data_root=data_root)
        out_dir.mkdir(parents=True, exist_ok=True)
        frame = pl.DataFrame(
            {"fixture_id": [1, 2], "kickoff_time": ["2025-08-16T14:00:00Z", None]}
        ).with_columns(
            pl.col("kickoff_time").str.strptime(
                pl.Datetime(time_unit="us", time_zone="UTC"), strict=False
            )
        )
        write_parquet(frame, out_dir / "part.parquet")

        assert rating_dates_for_season(SEASON, data_root=data_root) == [date(2025, 8, 15)]

    def test_absent_facts_table_yields_no_dates(self, tmp_path: Path) -> None:
        """Facts are built before the backfill runs; an unbuilt season is an
        ordering state, not an error."""
        assert rating_dates_for_season(SEASON, data_root=tmp_path / "data") == []


class TestCapturedDates:
    def test_reads_params_date_from_existing_partitions(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(data_root, ["2025-08-16T14:00:00Z"])
        backfill_clubelo_ratings([SEASON], connector=FakeConnector(), data_root=data_root)

        assert captured_dates(SEASON, data_root=data_root) == {date(2025, 8, 15)}

    def test_no_partitions_yields_empty(self, tmp_path: Path) -> None:
        assert captured_dates(SEASON, data_root=tmp_path / "data") == set()

    def test_partition_without_a_recorded_date_is_not_counted(self, tmp_path: Path) -> None:
        """Treating an unknown partition as captured would leave a permanent
        hole; re-fetching one date costs seconds."""
        data_root = tmp_path / "data"
        partition = (
            paths.raw_endpoint_dir("clubelo", "ratings", SEASON, data_root=data_root)
            / "as_of=2026-08-03T11-45-55Z"
        )
        partition.mkdir(parents=True)
        (partition / "meta.json").write_text(json.dumps({"params": {}}), encoding="utf-8")

        assert captured_dates(SEASON, data_root=data_root) == set()

    def test_unreadable_meta_is_skipped_rather_than_raising(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        partition = (
            paths.raw_endpoint_dir("clubelo", "ratings", SEASON, data_root=data_root)
            / "as_of=2026-08-03T11-45-55Z"
        )
        partition.mkdir(parents=True)
        (partition / "meta.json").write_text("{not json", encoding="utf-8")

        assert captured_dates(SEASON, data_root=data_root) == set()


class TestBackfillFetchesWhatIsMissing:
    def test_fetches_every_date_on_a_cold_run(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(data_root, ["2025-08-16T14:00:00Z", "2025-08-23T14:00:00Z"])
        connector = FakeConnector()

        [outcome] = backfill_clubelo_ratings([SEASON], connector=connector, data_root=data_root)

        assert connector.requested == [date(2025, 8, 15), date(2025, 8, 22)]
        assert outcome.fetched == 2
        assert outcome.skipped == 0
        assert outcome.complete

    def test_writes_one_partition_per_date(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(data_root, ["2025-08-16T14:00:00Z", "2025-08-23T14:00:00Z"])

        backfill_clubelo_ratings([SEASON], connector=FakeConnector(), data_root=data_root)

        partitions = list(
            paths.iter_as_of_partitions("clubelo", "ratings", SEASON, data_root=data_root)
        )
        assert len(partitions) == 2

    def test_spans_multiple_seasons(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(data_root, ["2025-08-16T14:00:00Z"], season=Season(2025))
        _write_facts(data_root, ["2024-08-17T14:00:00Z"], season=Season(2024))
        connector = FakeConnector()

        outcomes = backfill_clubelo_ratings(
            [Season(2024), Season(2025)], connector=connector, data_root=data_root
        )

        assert [o.season for o in outcomes] == [Season(2024), Season(2025)]
        assert connector.requested == [date(2024, 8, 16), date(2025, 8, 15)]


class TestBackfillIsResumable:
    def test_a_second_run_fetches_nothing(self, tmp_path: Path) -> None:
        """The property the whole design turns on: re-running a completed
        backfill must cost zero requests, not 1,153."""
        data_root = tmp_path / "data"
        _write_facts(data_root, ["2025-08-16T14:00:00Z", "2025-08-23T14:00:00Z"])
        backfill_clubelo_ratings([SEASON], connector=FakeConnector(), data_root=data_root)

        second = FakeConnector()
        [outcome] = backfill_clubelo_ratings([SEASON], connector=second, data_root=data_root)

        assert second.requested == []
        assert outcome.fetched == 0
        assert outcome.skipped == 2
        assert outcome.complete

    def test_resume_fetches_only_the_remaining_dates(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            ["2025-08-16T14:00:00Z", "2025-08-23T14:00:00Z", "2025-08-30T14:00:00Z"],
        )
        backfill_clubelo_ratings(
            [SEASON], connector=FakeConnector(), data_root=data_root, limit=1
        )

        second = FakeConnector()
        [outcome] = backfill_clubelo_ratings([SEASON], connector=second, data_root=data_root)

        assert second.requested == [date(2025, 8, 22), date(2025, 8, 29)]
        assert outcome.skipped == 1

    def test_identical_bodies_across_dates_still_produce_distinct_partitions(
        self, tmp_path: Path
    ) -> None:
        """Two consecutive dates often carry byte-identical ratings when no
        match was played between them. ``write_raw``'s content-hash skip would
        drop the second, so resumability must not be inferred from partition
        count alone — it is asserted here that both dates are recorded."""
        data_root = tmp_path / "data"
        _write_facts(data_root, ["2025-08-16T14:00:00Z", "2025-08-17T14:00:00Z"])

        class ConstantConnector(FakeConnector):
            def fetch_ratings(self, as_of_date: date) -> bytes:
                self.requested.append(as_of_date)
                return RATINGS_CSV

        backfill_clubelo_ratings([SEASON], connector=ConstantConnector(), data_root=data_root)
        second = FakeConnector()
        backfill_clubelo_ratings([SEASON], connector=second, data_root=data_root)

        assert second.requested == []


class TestBackfillHandlesFailures:
    def test_one_failure_does_not_abort_the_run(self, tmp_path: Path) -> None:
        """A single transient failure ending a two-hour run would be a poor
        trade against retrying one date later."""
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            ["2025-08-16T14:00:00Z", "2025-08-23T14:00:00Z", "2025-08-30T14:00:00Z"],
        )
        connector = FakeConnector(fail_on={date(2025, 8, 22)})

        [outcome] = backfill_clubelo_ratings([SEASON], connector=connector, data_root=data_root)

        assert outcome.fetched == 2
        assert [day for day, _ in outcome.failed] == [date(2025, 8, 22)]
        assert not outcome.complete

    def test_a_failed_date_is_retried_on_the_next_run(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(data_root, ["2025-08-16T14:00:00Z", "2025-08-23T14:00:00Z"])
        backfill_clubelo_ratings(
            [SEASON],
            connector=FakeConnector(fail_on={date(2025, 8, 22)}),
            data_root=data_root,
        )

        second = FakeConnector()
        [outcome] = backfill_clubelo_ratings([SEASON], connector=second, data_root=data_root)

        assert second.requested == [date(2025, 8, 22)]
        assert outcome.complete


class TestBackfillLimit:
    def test_limit_caps_total_fetches_across_seasons(self, tmp_path: Path) -> None:
        """Makes a short live smoke test possible without committing to the
        full two-hour run."""
        data_root = tmp_path / "data"
        _write_facts(data_root, ["2024-08-17T14:00:00Z", "2024-08-24T14:00:00Z"], Season(2024))
        _write_facts(data_root, ["2025-08-16T14:00:00Z"], Season(2025))
        connector = FakeConnector()

        backfill_clubelo_ratings(
            [Season(2024), Season(2025)], connector=connector, data_root=data_root, limit=2
        )

        assert len(connector.requested) == 2

    def test_no_limit_fetches_everything(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(data_root, ["2025-08-16T14:00:00Z", "2025-08-23T14:00:00Z"])
        connector = FakeConnector()

        backfill_clubelo_ratings([SEASON], connector=connector, data_root=data_root)

        assert len(connector.requested) == 2


class TestDryRunCosting:
    def test_reports_dates_in_scope_per_season(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(data_root, ["2025-08-16T14:00:00Z", "2025-08-23T14:00:00Z"], Season(2025))
        _write_facts(data_root, ["2024-08-17T14:00:00Z"], Season(2024))

        counts = total_dates_in_scope([Season(2024), Season(2025)], data_root=data_root)

        assert counts == {Season(2024): 1, Season(2025): 2}
