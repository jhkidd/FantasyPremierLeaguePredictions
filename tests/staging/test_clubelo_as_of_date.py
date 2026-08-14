"""Regression tests for Club Elo ``as_of_date`` stamping (plan §0.5, BUG 3).

Club Elo's API is ``http://api.clubelo.com/<date>`` — the URL names the date
the ratings are *for*, and the connector records it in ``meta.json`` under
``params.date``. Staging, however, stamped ``as_of_date`` from the *partition
directory name*, which encodes when the fetch happened.

For a live daily pull those two coincide, which is why this survived review.
For any historical pull they do not: the 2025-26 season's ratings were
requested for 2026-05-15 but fetched on 2026-08-03, and were therefore staged
three months in the future. ``facts/team_fixture`` picks a rating with
``as_of_date <= kickoff``, so every fixture in the season found nothing and
``elo_rating`` came out 100% null across all ten seasons.

The failure is worse than it sounds for a backfill: a thousand historical
dates fetched in one run would all be stamped with that run's date, collapsing
a decade of distinct ratings into a handful of duplicate-keyed days.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from fpl.config import Season
from fpl.staging.pipeline import stage_clubelo_source
from fpl.storage.raw_io import RawArtifact, write_raw

SEASON = Season(2025)

RATINGS_CSV = (
    b"Rank,Club,Country,Level,Elo,From,To\n"
    b"1,Arsenal,ENG,1,2063.7578125,2025-05-31,2025-08-21\n"
    b"2,Man City,ENG,1,2029.451171875,2025-05-31,2025-08-21\n"
)


def _write_capture(
    data_root: Path,
    *,
    fetched_at: datetime,
    rating_date: str | None,
    body: bytes = RATINGS_CSV,
    season: Season = SEASON,
) -> None:
    """Store one raw capture, optionally recording the date it was requested for.

    ``rating_date is None`` reproduces a capture written before the connector
    recorded ``params.date`` — those partitions exist on disk already and must
    keep staging rather than raising.
    """
    artifact = RawArtifact(
        source="clubelo",
        endpoint="ratings",
        season=season,
        url=f"http://api.clubelo.com/{rating_date or fetched_at.date().isoformat()}",
        http_status=200,
        body=body,
        fetched_at=fetched_at,
        connector_version="1",
        content_type="csv",
        params={"date": rating_date} if rating_date else {},
    )
    write_raw(artifact, data_root=data_root)


def _staged(data_root: Path, season: Season = SEASON) -> pl.DataFrame:
    path = data_root / "staged" / "clubelo_ratings" / f"season={season}" / "part.parquet"
    return pl.read_parquet(path)


class TestAsOfDateComesFromTheRequestedDate:
    def test_uses_params_date_not_the_fetch_date(self, tmp_path: Path) -> None:
        """The headline repair. Fetched in August, requested for May — the
        rating is May's, and stamping it August makes it invisible to every
        fixture in the season."""
        data_root = tmp_path / "data"
        _write_capture(
            data_root,
            fetched_at=datetime(2026, 8, 3, 11, 45, tzinfo=UTC),
            rating_date="2026-05-15",
        )

        stage_clubelo_source(SEASON, data_root=data_root)

        stamps = set(_staged(data_root)["as_of_date"].to_list())
        assert stamps == {"2026-05-15"}

    def test_a_backfill_run_keeps_each_date_distinct(self, tmp_path: Path) -> None:
        """Three dates pulled in a single backfill run share a fetch date but
        are three different ratings — stamping by fetch time would collapse
        them onto one key and silently drop two thirds of the history."""
        data_root = tmp_path / "data"
        run = datetime(2026, 8, 3, 11, 45, tzinfo=UTC)
        for offset, rating_date in enumerate(["2025-08-01", "2025-09-01", "2025-10-01"]):
            _write_capture(
                data_root,
                fetched_at=run.replace(minute=45 + offset),
                rating_date=rating_date,
                body=RATINGS_CSV.replace(b"2063.75", f"20{60 + offset}.75".encode()),
            )

        stage_clubelo_source(SEASON, data_root=data_root)

        frame = _staged(data_root)
        assert sorted(set(frame["as_of_date"].to_list())) == [
            "2025-08-01",
            "2025-09-01",
            "2025-10-01",
        ]
        assert frame.height == 6

    def test_stamped_date_lies_inside_the_ratings_validity_window(self, tmp_path: Path) -> None:
        """``valid_from``/``valid_to`` are Club Elo's own statement of which
        days a rating describes. A correctly stamped row falls inside them;
        the fetch-date bug put every row months outside."""
        data_root = tmp_path / "data"
        _write_capture(
            data_root,
            fetched_at=datetime(2026, 8, 3, tzinfo=UTC),
            rating_date="2025-06-15",
        )

        stage_clubelo_source(SEASON, data_root=data_root)

        frame = _staged(data_root).with_columns(
            pl.col("as_of_date").str.strptime(pl.Date).alias("_as_of"),
            pl.col("valid_from").str.strptime(pl.Date).alias("_from"),
            pl.col("valid_to").str.strptime(pl.Date).alias("_to"),
        )
        outside = frame.filter(
            (pl.col("_as_of") < pl.col("_from")) | (pl.col("_as_of") > pl.col("_to"))
        )
        assert outside.height == 0


class TestCapturesWithoutARecordedDate:
    def test_falls_back_to_the_partition_timestamp(self, tmp_path: Path) -> None:
        """Partitions captured before ``params.date`` was recorded must keep
        staging — refusing them would strand data already on disk."""
        data_root = tmp_path / "data"
        _write_capture(
            data_root, fetched_at=datetime(2025, 8, 15, tzinfo=UTC), rating_date=None
        )

        stage_clubelo_source(SEASON, data_root=data_root)

        assert set(_staged(data_root)["as_of_date"].to_list()) == {"2025-08-15"}

    def test_malformed_recorded_date_is_rejected_loudly(self, tmp_path: Path) -> None:
        """Silently falling back on a *corrupt* date would reintroduce the
        original bug under a different name, so it raises instead."""
        data_root = tmp_path / "data"
        _write_capture(
            data_root, fetched_at=datetime(2025, 8, 15, tzinfo=UTC), rating_date="not-a-date"
        )

        with pytest.raises(ValueError, match="not-a-date"):
            stage_clubelo_source(SEASON, data_root=data_root)

