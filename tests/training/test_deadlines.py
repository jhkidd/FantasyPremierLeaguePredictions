"""Tests for :mod:`fpl.training.deadlines`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from fpl.config import Season
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet
from fpl.training.deadlines import gameweek_deadlines

SEASON = Season(2025)


def _write_facts(data_root: Path, rows: list[dict]) -> None:
    frame = pl.DataFrame(rows)
    out_dir = paths.facts_table("player_fixture", SEASON, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet")


def _kickoff(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _row(fixture_id: int, event: int, kickoff: datetime) -> dict:
    return {
        "season": str(SEASON),
        "fixture_id": fixture_id,
        "player_id": 1,
        "event": event,
        "kickoff_time": kickoff,
    }


class TestGameweekDeadlines:
    """A gameweek's deadline is its earliest kickoff minus one hour."""

    def test_single_gameweek_single_fixture(self, tmp_path: Path) -> None:
        _write_facts(
            tmp_path,
            [_row(1, 1, _kickoff("2025-08-16T15:00:00"))],
        )

        deadlines = gameweek_deadlines(SEASON, data_root=tmp_path)

        assert deadlines == {1: _kickoff("2025-08-16T14:00:00")}

    def test_gameweek_deadline_is_earliest_of_several_fixtures(self, tmp_path: Path) -> None:
        _write_facts(
            tmp_path,
            [
                _row(1, 1, _kickoff("2025-08-16T15:00:00")),
                _row(2, 1, _kickoff("2025-08-16T12:30:00")),
                _row(3, 1, _kickoff("2025-08-17T14:00:00")),
            ],
        )

        deadlines = gameweek_deadlines(SEASON, data_root=tmp_path)

        assert deadlines == {1: _kickoff("2025-08-16T11:30:00")}

    def test_multiple_gameweeks(self, tmp_path: Path) -> None:
        _write_facts(
            tmp_path,
            [
                _row(1, 1, _kickoff("2025-08-16T15:00:00")),
                _row(2, 2, _kickoff("2025-08-23T15:00:00")),
            ],
        )

        deadlines = gameweek_deadlines(SEASON, data_root=tmp_path)

        assert deadlines == {
            1: _kickoff("2025-08-16T14:00:00"),
            2: _kickoff("2025-08-23T14:00:00"),
        }

    def test_duplicate_player_fixture_rows_do_not_change_the_deadline(self, tmp_path: Path) -> None:
        """Every player in a fixture repeats its kickoff_time; the deadline
        must be computed per distinct fixture/event, not per row."""
        rows = [_row(1, 1, _kickoff("2025-08-16T15:00:00")) | {"player_id": p} for p in range(20)]
        _write_facts(tmp_path, rows)

        deadlines = gameweek_deadlines(SEASON, data_root=tmp_path)

        assert deadlines == {1: _kickoff("2025-08-16T14:00:00")}

    def test_no_staged_facts_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            gameweek_deadlines(SEASON, data_root=tmp_path)

    def test_overlapping_gameweeks_raise(self, tmp_path: Path) -> None:
        """Gameweek 1 runs 2025-08-16 -> 2025-08-30 (a rearranged fixture),
        overlapping gameweek 2's 2025-08-23 kickoff. This is a real failure
        mode: postponed/rearranged matches can be played weeks after their
        nominal gameweek, at which point 'min(kickoff_time) per event' is no
        longer a safe way to derive a deadline."""
        _write_facts(
            tmp_path,
            [
                _row(1, 1, _kickoff("2025-08-16T15:00:00")),
                _row(2, 1, _kickoff("2025-08-30T15:00:00")),  # rearranged into GW2's window
                _row(3, 2, _kickoff("2025-08-23T15:00:00")),
            ],
        )

        with pytest.raises(ValueError, match="overlap"):
            gameweek_deadlines(SEASON, data_root=tmp_path)

    def test_non_adjacent_overlap_is_still_caught(self, tmp_path: Path) -> None:
        """GW1 spans 2025-08-16 -> 2025-10-01, engulfing both GW2 and GW3's
        windows entirely. A naive 'only compare consecutive events' check
        would miss this; the running-max-end sweep must not."""
        _write_facts(
            tmp_path,
            [
                _row(1, 1, _kickoff("2025-08-16T15:00:00")),
                _row(2, 1, _kickoff("2025-10-01T15:00:00")),
                _row(3, 2, _kickoff("2025-08-23T15:00:00")),
                _row(4, 3, _kickoff("2025-08-30T15:00:00")),
            ],
        )

        with pytest.raises(ValueError, match="overlap"):
            gameweek_deadlines(SEASON, data_root=tmp_path)

    def test_null_event_rows_are_ignored(self, tmp_path: Path) -> None:
        _write_facts(
            tmp_path,
            [
                _row(1, 1, _kickoff("2025-08-16T15:00:00")),
                {**_row(2, None, _kickoff("2025-08-17T15:00:00")), "event": None},
            ],
        )

        deadlines = gameweek_deadlines(SEASON, data_root=tmp_path)

        assert deadlines == {1: _kickoff("2025-08-16T14:00:00")}
