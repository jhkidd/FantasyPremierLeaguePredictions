"""Tests for staging vaastav's ``teams.csv`` / ``fixtures.csv``, and for the
two fallbacks that cover the seasons the archive never published them for.

The archive begins ``fixtures.csv`` at 2018/19 and ``teams.csv`` at 2019/20
(confirmed 404 upstream for the earlier seasons), while the FPL API serves
only the current season. Both fallbacks are therefore the *only* route to a
fixture calendar and a stable ``team_code`` for the earliest seasons, without
which ``facts/team_fixture`` cannot be built at all for a quarter of the
available training data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from fpl.config import Season
from fpl.identity.teams_from_matches import derive_teams_from_frames
from fpl.staging.fixtures_from_facts import fixtures_from_player_stats
from fpl.staging.pipeline import stage_vaastav_fixtures, stage_vaastav_teams
from fpl.staging.vaastav import stage_fixtures_csv, stage_teams_csv
from fpl.storage import paths
from fpl.storage.raw_io import RawArtifact, write_raw

SEASON = Season(2021)

TEAMS_CSV = (
    b"code,draw,form,id,loss,name,played,points,position,short_name,strength\n"
    b"3,0,,1,0,Arsenal,0,0,1,ARS,4\n"
    b"7,0,,2,0,Aston Villa,0,0,2,AVL,3\n"
)

FIXTURES_CSV = (
    b"code,event,finished,id,kickoff_time,minutes,started,stats,"
    b"team_a,team_a_score,team_h,team_h_score\n"
    b"2128288,1,True,1,2021-08-13T19:00:00Z,90,True,[],2,0,1,2\n"
)


def _write_raw_csv(data_root: Path, endpoint: str, body: bytes, season: Season = SEASON) -> None:
    write_raw(
        RawArtifact(
            source="vaastav",
            endpoint=endpoint,
            season=season,
            url=f"https://github.com/vaastav/Fantasy-Premier-League/.../{endpoint}.csv",
            http_status=200,
            body=body,
            fetched_at=datetime(2026, 8, 13, tzinfo=UTC),
            connector_version="1",
            content_type="csv",
        ),
        data_root=data_root,
    )


class TestStageTeamsCsv:
    def test_maps_the_fpl_teams_spec_columns(self) -> None:
        frame, _report = stage_teams_csv(TEAMS_CSV, SEASON)
        row = frame.row(0, named=True)
        assert row["season"] == "2021-22"
        assert row["team_id"] == 1
        assert row["code"] == 3
        assert row["name"] == "Arsenal"
        assert row["short_name"] == "ARS"

    def test_carries_the_stable_cross_season_code(self) -> None:
        """``code`` is the whole point: ``team_id`` is reassigned every season,
        so it cannot join anything across seasons."""
        frame, _report = stage_teams_csv(TEAMS_CSV, SEASON)
        assert frame.sort("team_id")["code"].to_list() == [3, 7]

    def test_form_is_dropped_not_imported(self) -> None:
        frame, _report = stage_teams_csv(TEAMS_CSV, SEASON)
        assert "form" not in frame.columns


class TestStageFixturesCsv:
    def test_maps_the_fpl_fixtures_spec_columns(self) -> None:
        frame, _report = stage_fixtures_csv(FIXTURES_CSV, SEASON)
        row = frame.row(0, named=True)
        assert row["fixture_id"] == 1
        assert row["team_h"] == 1
        assert row["team_a"] == 2
        assert row["team_h_score"] == 2
        assert row["team_a_score"] == 0
        assert row["finished"] is True

    def test_nested_stats_blob_is_dropped(self) -> None:
        frame, _report = stage_fixtures_csv(FIXTURES_CSV, SEASON)
        assert "stats" not in frame.columns


class TestStageVaastavTeams:
    def test_reports_no_capture_rather_than_raising(self, tmp_path: Path) -> None:
        (result,) = stage_vaastav_teams(SEASON, data_root=tmp_path / "data")
        assert not result.written
        assert "no vaastav teams capture" in result.detail

    def test_writes_the_staged_table(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_raw_csv(data_root, "teams", TEAMS_CSV)

        (result,) = stage_vaastav_teams(SEASON, data_root=data_root)

        assert result.written
        assert result.rows == 2
        assert (paths.staged_table("teams", SEASON, data_root=data_root) / "part.parquet").exists()


class TestStageVaastavFixtures:
    def test_reports_no_capture_rather_than_raising(self, tmp_path: Path) -> None:
        (result,) = stage_vaastav_fixtures(SEASON, data_root=tmp_path / "data")
        assert not result.written
        assert "reconstruct" in result.detail

    def test_writes_the_staged_table(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_raw_csv(data_root, "fixtures", FIXTURES_CSV)

        (result,) = stage_vaastav_fixtures(SEASON, data_root=data_root)

        assert result.written
        assert result.rows == 1


def _player_stats(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def _stats_row(
    *, fixture_id: int, player_id: int, opponent: int, was_home: bool, h: int, a: int
) -> dict:
    return {
        "fixture_id": fixture_id,
        "player_id": player_id,
        "opponent_team": opponent,
        "was_home": was_home,
        "event": 1,
        "kickoff_time": "2016-08-13T14:00:00Z",
        "minutes": 90,
        "team_h_score": h,
        "team_a_score": a,
    }


class TestFixturesFromPlayerStats:
    def test_derives_home_and_away_from_the_opponent_column(self) -> None:
        """A home player's opponent *is* the away team, so both ids fall out
        of the opponent column without needing ``team_id`` resolved first."""
        stats = _player_stats(
            [
                _stats_row(fixture_id=1, player_id=1, opponent=7, was_home=True, h=2, a=1),
                _stats_row(fixture_id=1, player_id=2, opponent=3, was_home=False, h=2, a=1),
            ]
        )

        frame, _report = fixtures_from_player_stats(stats, Season(2016))

        row = frame.row(0, named=True)
        assert row["team_h"] == 3
        assert row["team_a"] == 7
        assert row["team_h_score"] == 2
        assert row["team_a_score"] == 1
        assert row["finished"] is True

    def test_one_row_per_fixture(self) -> None:
        stats = _player_stats(
            [
                _stats_row(fixture_id=1, player_id=1, opponent=7, was_home=True, h=2, a=1),
                _stats_row(fixture_id=1, player_id=2, opponent=7, was_home=True, h=2, a=1),
                _stats_row(fixture_id=1, player_id=3, opponent=3, was_home=False, h=2, a=1),
                _stats_row(fixture_id=2, player_id=4, opponent=9, was_home=True, h=0, a=0),
                _stats_row(fixture_id=2, player_id=5, opponent=3, was_home=False, h=0, a=0),
            ]
        )

        frame, _report = fixtures_from_player_stats(stats, Season(2016))

        assert frame.height == 2
        assert frame["fixture_id"].to_list() == [1, 2] or set(frame["fixture_id"]) == {1, 2}

    def test_code_is_null_not_invented(self) -> None:
        """FPL's cross-season fixture ``code`` is genuinely unrecoverable
        here, so it must be absent rather than fabricated."""
        stats = _player_stats(
            [
                _stats_row(fixture_id=1, player_id=1, opponent=7, was_home=True, h=2, a=1),
                _stats_row(fixture_id=1, player_id=2, opponent=3, was_home=False, h=2, a=1),
            ]
        )

        frame, _report = fixtures_from_player_stats(stats, Season(2016))

        assert frame["code"].null_count() == frame.height


def _crosswalk() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "team_code": ["3", "7"],
            "footballdata_couk_name": ["Arsenal", "Aston Villa"],
        }
    )


def _fixtures(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows).with_columns(
        pl.col("kickoff_time").str.strptime(
            pl.Datetime(time_unit="us", time_zone="UTC"), strict=False
        )
    )


class TestDeriveTeamsFromMatches:
    def _one_season(self, *, clubs: int = 2) -> tuple[pl.DataFrame, pl.DataFrame]:
        fixtures = _fixtures(
            [
                {
                    "fixture_id": 1,
                    "kickoff_time": "2016-08-13T14:00:00Z",
                    "team_h": 1,
                    "team_a": 2,
                    "team_h_score": 2,
                    "team_a_score": 1,
                }
            ]
        )
        matches = pl.DataFrame(
            {
                "match_date": [datetime(2016, 8, 13).date()],
                "home_team": ["Arsenal"],
                "away_team": ["Aston Villa"],
                "full_time_home_goals": [2],
                "full_time_away_goals": [1],
            }
        )
        return fixtures, matches

    def test_refuses_a_partial_mapping(self) -> None:
        """A season must resolve all 20 clubs or none — a partial teams table
        would silently drop whichever clubs failed to align."""
        fixtures, matches = self._one_season()

        with pytest.raises(ValueError, match="refusing to write a partial teams table"):
            derive_teams_from_frames(fixtures, matches, _crosswalk(), Season(2016))

    def test_unaligned_fixtures_are_counted(self) -> None:
        fixtures, matches = self._one_season()
        fixtures = fixtures.with_columns(pl.lit(9).alias("team_h_score"))

        with pytest.raises(ValueError):
            derive_teams_from_frames(fixtures, matches, _crosswalk(), Season(2016))
