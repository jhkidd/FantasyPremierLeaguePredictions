"""Tests for ``team_code``/``opponent_team_code`` on ``player_fixture`` (Step 9).

``team_id`` is reassigned every season — FPL numbers the twenty clubs
alphabetically at the start of each campaign, so id 3 names a different club in
2020/21, 2022/23 and 2025/26. Any feature that keys a club across seasons
(rolling form, promoted-side flags, opponent strength priors) therefore needs
the stable ``code``, which is why these two columns are carried on the fact
table rather than re-joined by every consumer.

The join is deliberately *optional*: a missing ``teams`` table leaves both
columns null and logs, rather than failing the build. Making a facts build hard
depend on another source having been staged first is exactly the coupling that
produced the all-null ``team_id`` bug repaired in Steps 1-4.
"""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.facts.player_fixture import build_player_fixture_facts
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet

SEASON = Season(2021)

_HOME_TEAM_ID = 17
_AWAY_TEAM_ID = 19
_HOME_TEAM_CODE = 43
_AWAY_TEAM_CODE = 8


def _stats_row(
    *,
    player_id: int,
    fixture_id: int,
    opponent_team_id: int,
    was_home: bool,
    team_name: str,
) -> dict:
    return {
        "season": str(SEASON),
        "player_name": f"Player {player_id}",
        "position": "MID",
        "team": team_name,
        "player_id": player_id,
        "fixture_id": fixture_id,
        "event": 1,
        "kickoff_time": "2021-08-14T14:00:00Z",
        "was_home": was_home,
        "opponent_team": opponent_team_id,
        "minutes": 90,
        "starts": 1,
        "goals_scored": 0,
        "assists": 0,
        "goals_conceded": 0,
        "own_goals": 0,
        "penalties_saved": 0,
        "penalties_missed": 0,
        "yellow_cards": 0,
        "red_cards": 0,
        "saves": 0,
        "bonus_fpl": 0,
        "bps_fpl": 10,
        "total_points_fpl": 2,
    }


def _write_stats(data_root: Path, rows: list[dict]) -> None:
    out_dir = paths.staged_table("player_fixture_stats", SEASON, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(pl.DataFrame(rows), out_dir / "part.parquet")


def _write_teams(data_root: Path, rows: list[dict]) -> None:
    out_dir = paths.staged_table("teams", SEASON, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(pl.DataFrame(rows), out_dir / "part.parquet")


def _teams_row(team_id: int, code: int, name: str) -> dict:
    return {
        "season": str(SEASON),
        "team_id": team_id,
        "code": code,
        "name": name,
        "short_name": name[:3].upper(),
        "strength": 3,
    }


def _fixture(fixture_id: int = 100) -> list[dict]:
    return [
        _stats_row(
            player_id=1,
            fixture_id=fixture_id,
            opponent_team_id=_AWAY_TEAM_ID,
            was_home=True,
            team_name="Home FC",
        ),
        _stats_row(
            player_id=2,
            fixture_id=fixture_id,
            opponent_team_id=_HOME_TEAM_ID,
            was_home=False,
            team_name="Away FC",
        ),
    ]


def _both_teams() -> list[dict]:
    return [
        _teams_row(_HOME_TEAM_ID, _HOME_TEAM_CODE, "Home FC"),
        _teams_row(_AWAY_TEAM_ID, _AWAY_TEAM_CODE, "Away FC"),
    ]


class TestTeamCodeJoin:
    def test_own_and_opponent_codes_are_populated(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture())
        _write_teams(data_root, _both_teams())

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        assert facts is not None
        by_player = {row["player_id"]: row for row in facts.iter_rows(named=True)}
        assert by_player[1]["team_code"] == _HOME_TEAM_CODE
        assert by_player[1]["opponent_team_code"] == _AWAY_TEAM_CODE
        assert by_player[2]["team_code"] == _AWAY_TEAM_CODE
        assert by_player[2]["opponent_team_code"] == _HOME_TEAM_CODE

    def test_codes_follow_derived_team_id_not_the_team_name(self, tmp_path: Path) -> None:
        """The join keys off the derived ``team_id``, so a wrong ``team``
        name string in the raw data cannot corrupt the code."""
        data_root = tmp_path / "data"
        rows = _fixture()
        rows[0]["team"] = "Nonsense United"
        _write_stats(data_root, rows)
        _write_teams(data_root, _both_teams())

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        row = facts.filter(pl.col("player_id") == 1).row(0, named=True)
        assert row["team_code"] == _HOME_TEAM_CODE

    def test_no_row_is_its_own_opponent_by_code(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture())
        _write_teams(data_root, _both_teams())

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        assert facts.filter(pl.col("team_code") == pl.col("opponent_team_code")).height == 0

    def test_row_count_is_unchanged_by_the_join(self, tmp_path: Path) -> None:
        """A left join against a table with a duplicated ``team_id`` would
        silently fan out rows and break the primary key."""
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture())
        _write_teams(data_root, _both_teams())

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        assert facts.height == 2


class TestTeamCodeIsOptional:
    def test_missing_teams_table_leaves_codes_null(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture())

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        assert facts is not None
        assert facts["team_code"].null_count() == facts.height
        assert facts["opponent_team_code"].null_count() == facts.height

    def test_missing_teams_table_still_derives_team_id(self, tmp_path: Path) -> None:
        """The two repairs are independent: losing the code must not cost us
        the ``team_id`` fix, which needs no external table at all."""
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture())

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        assert facts["team_id"].null_count() == 0

    def test_missing_teams_table_is_logged_with_a_row_count(self, tmp_path: Path, caplog) -> None:
        """Silence here would let a whole season lose its cross-season key
        without anyone noticing until model training."""
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture())

        with caplog.at_level(logging.WARNING, logger="fpl.facts.player_fixture"):
            build_player_fixture_facts(SEASON, data_root=data_root)

        assert any("teams" in record.message for record in caplog.records)
        assert any("2" in record.getMessage() for record in caplog.records)

    def test_team_absent_from_teams_table_leaves_that_row_null(self, tmp_path: Path) -> None:
        """A partial ``teams`` table nulls only the rows it cannot resolve,
        rather than dropping them — losing observations would silently bias
        anything trained downstream."""
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture())
        _write_teams(data_root, [_teams_row(_HOME_TEAM_ID, _HOME_TEAM_CODE, "Home FC")])

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        assert facts.height == 2
        by_player = {row["player_id"]: row for row in facts.iter_rows(named=True)}
        assert by_player[1]["team_code"] == _HOME_TEAM_CODE
        assert by_player[1]["opponent_team_code"] is None
        assert by_player[2]["team_code"] is None
        assert by_player[2]["opponent_team_code"] == _HOME_TEAM_CODE


class TestTeamCodeSchema:
    def test_columns_are_present_and_integer_typed(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture())
        _write_teams(data_root, _both_teams())

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        assert facts.schema["team_code"] == pl.Int64
        assert facts.schema["opponent_team_code"] == pl.Int64

    def test_columns_are_present_even_when_null(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture())

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        assert facts.schema["team_code"] == pl.Int64
        assert facts.schema["opponent_team_code"] == pl.Int64
