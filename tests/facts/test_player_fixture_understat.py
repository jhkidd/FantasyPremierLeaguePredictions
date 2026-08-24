"""Tests for the Understat -> ``facts/player_fixture`` join (Phase B, step 1).

Understat's own ``match_id`` has no crosswalk to FPL's ``fixture_id``, so the
join resolves through team identity instead: each side's ``team_code`` is
mapped to Understat's own team-name string via the reviewed
``crosswalk/team_external_ids.csv``, and Understat's per-match ``match_id`` is
looked up by ``(season, home_team_code, away_team_code)`` - unambiguous
because two teams meet at most once a season in the Premier League (no
replays), and immune to postponement-driven date drift because date is not
part of the key at all.

Player identity resolves through the existing
``crosswalk/players_fpl_understat.csv`` (``player_code`` <->
``understat_player_id``) - already built for a different purpose (plan
§7.10) and reused here unchanged.

A row with no Understat match (unmapped player, or a fixture Understat never
covered) keeps all ``understat_*`` columns null and ``obs_understat`` false -
this join is purely additive, never row-dropping.
"""

from __future__ import annotations

import polars as pl
import pytest

from fpl.config import Season
from fpl.facts.player_fixture import build_player_fixture_facts
from fpl.identity.players_understat import write_players_crosswalk
from fpl.identity.team_external_ids import write_team_external_ids
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet

SEASON = Season(2021)

_HOME_TEAM_ID = 17
_AWAY_TEAM_ID = 19
_HOME_TEAM_CODE = 43
_AWAY_TEAM_CODE = 8

_MATCHED_PLAYER_ID = 1
_UNMATCHED_PLAYER_ID = 2
_UNDERSTAT_PLAYER_ID = 9001
_MATCH_ID = 55555


def _stats_row(
    *,
    player_id: int,
    player_code: str,
    fixture_id: int,
    opponent_team_id: int,
    was_home: bool,
) -> dict:
    return {
        "season": str(SEASON),
        "player_name": f"Player {player_id}",
        "position": "MID",
        "team": "Home FC" if was_home else "Away FC",
        "player_id": player_id,
        "player_code": player_code,
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


def _write_stats(data_root, rows: list[dict]) -> None:
    out_dir = paths.staged_table("player_fixture_stats", SEASON, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(pl.DataFrame(rows), out_dir / "part.parquet")


def _write_teams(data_root) -> None:
    out_dir = paths.staged_table("teams", SEASON, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "season": str(SEASON),
            "team_id": _HOME_TEAM_ID,
            "code": _HOME_TEAM_CODE,
            "name": "Home FC",
            "short_name": "HOM",
            "strength": 3,
        },
        {
            "season": str(SEASON),
            "team_id": _AWAY_TEAM_ID,
            "code": _AWAY_TEAM_CODE,
            "name": "Away FC",
            "short_name": "AWY",
            "strength": 3,
        },
    ]
    write_parquet(pl.DataFrame(rows), out_dir / "part.parquet")


def _write_team_external_ids(data_root) -> None:
    crosswalk = pl.DataFrame(
        {
            "team_code": [str(_HOME_TEAM_CODE), str(_AWAY_TEAM_CODE)],
            "clubelo_name": [None, None],
            "understat_name": ["Home United", "Away Town"],
            "footballdata_couk_name": [None, None],
            "openfootball_name": [None, None],
        }
    )
    write_team_external_ids(crosswalk, data_root=data_root)


def _write_players_understat_crosswalk(data_root, *, matched: bool = True) -> None:
    if matched:
        crosswalk = pl.DataFrame(
            {
                "player_code": ["P1"],
                "fpl_name": ["Player 1"],
                "understat_player_id": [_UNDERSTAT_PLAYER_ID],
                "understat_name": ["Player One"],
            }
        )
    else:
        crosswalk = pl.DataFrame(
            {
                "player_code": [],
                "fpl_name": [],
                "understat_player_id": [],
                "understat_name": [],
            },
            schema={
                "player_code": pl.Utf8,
                "fpl_name": pl.Utf8,
                "understat_player_id": pl.Int64,
                "understat_name": pl.Utf8,
            },
        )
    write_players_crosswalk(crosswalk, data_root=data_root)


def _write_understat_fixtures(data_root, *, home_name="Home United", away_name="Away Town") -> None:
    out_dir = paths.staged_table("understat_fixtures", SEASON, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "season": str(SEASON),
            "match_id": _MATCH_ID,
            "is_result": True,
            "datetime": "2021-08-14 14:00:00",
            "home_team": home_name,
            "away_team": away_name,
            "home_goals": 2,
            "away_goals": 1,
            "home_xg": 1.8,
            "away_xg": 0.9,
        }
    ]
    write_parquet(pl.DataFrame(rows), out_dir / "part.parquet")


def _write_understat_player_match(data_root) -> None:
    out_dir = paths.staged_table("understat_player_match", SEASON, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "season": str(SEASON),
            "match_id": _MATCH_ID,
            "side": "h",
            "player_id": _UNDERSTAT_PLAYER_ID,
            "player_name": "Player One",
            "team_id": 111,
            "position": "M",
            "minutes": 90,
            "goals": 1,
            "own_goals": 0,
            "shots": 3,
            "xg": 0.75,
            "assists": 0,
            "xa": 0.2,
            "key_passes": 2,
            "yellow_card": 0,
            "red_card": 0,
            "xg_chain": 0.9,
            "xg_buildup": 0.3,
        }
    ]
    write_parquet(pl.DataFrame(rows), out_dir / "part.parquet")


def _fixture_rows() -> list[dict]:
    return [
        _stats_row(
            player_id=_MATCHED_PLAYER_ID,
            player_code="P1",
            fixture_id=100,
            opponent_team_id=_AWAY_TEAM_ID,
            was_home=True,
        ),
        _stats_row(
            player_id=_UNMATCHED_PLAYER_ID,
            player_code="P2",
            fixture_id=100,
            opponent_team_id=_HOME_TEAM_ID,
            was_home=False,
        ),
    ]


class TestUnderstatJoin:
    def test_matched_row_gets_understat_columns_and_obs_true(self, tmp_path) -> None:
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture_rows())
        _write_teams(data_root)
        _write_team_external_ids(data_root)
        _write_players_understat_crosswalk(data_root, matched=True)
        _write_understat_fixtures(data_root)
        _write_understat_player_match(data_root)

        facts = build_player_fixture_facts(SEASON, data_root=data_root)
        assert facts is not None

        row = facts.filter(pl.col("player_id") == _MATCHED_PLAYER_ID).row(0, named=True)
        assert row["obs_understat"] is True
        assert row["understat_goals"] == 1
        assert row["understat_xg"] == pytest.approx(0.75)
        assert row["understat_xa"] == pytest.approx(0.2)
        assert row["understat_key_passes"] == 2
        assert row["understat_shots"] == 3
        assert row["understat_xg_chain"] == pytest.approx(0.9)
        assert row["understat_xg_buildup"] == pytest.approx(0.3)
        assert row["understat_yellow_card"] == 0
        assert row["understat_red_card"] == 0
        assert row["understat_own_goals"] == 0
        assert row["understat_assists"] == 0
        assert row["understat_minutes"] == 90

    def test_player_with_no_crosswalk_row_is_null_and_obs_false(self, tmp_path) -> None:
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture_rows())
        _write_teams(data_root)
        _write_team_external_ids(data_root)
        _write_players_understat_crosswalk(data_root, matched=True)
        _write_understat_fixtures(data_root)
        _write_understat_player_match(data_root)

        facts = build_player_fixture_facts(SEASON, data_root=data_root)
        assert facts is not None

        row = facts.filter(pl.col("player_id") == _UNMATCHED_PLAYER_ID).row(0, named=True)
        assert row["obs_understat"] is False
        assert row["understat_xg"] is None
        assert row["understat_goals"] is None

    def test_no_understat_data_at_all_leaves_columns_null(self, tmp_path) -> None:
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture_rows())
        _write_teams(data_root)
        # No team_external_ids, no players_understat crosswalk, no understat
        # staged tables at all - a normal, expected state (e.g. an early
        # season Understat hasn't been backfilled for yet).
        facts = build_player_fixture_facts(SEASON, data_root=data_root)
        assert facts is not None
        assert facts["obs_understat"].to_list() == [False, False]
        assert facts["understat_xg"].null_count() == 2

    def test_duplicate_team_pair_in_understat_fixtures_raises(self, tmp_path) -> None:
        data_root = tmp_path / "data"
        _write_stats(data_root, _fixture_rows())
        _write_teams(data_root)
        _write_team_external_ids(data_root)
        _write_players_understat_crosswalk(data_root, matched=True)

        out_dir = paths.staged_table("understat_fixtures", SEASON, data_root=data_root)
        out_dir.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "season": str(SEASON),
                "match_id": _MATCH_ID,
                "is_result": True,
                "datetime": "2021-08-14 14:00:00",
                "home_team": "Home United",
                "away_team": "Away Town",
                "home_goals": 2,
                "away_goals": 1,
                "home_xg": 1.8,
                "away_xg": 0.9,
            },
            {
                "season": str(SEASON),
                "match_id": _MATCH_ID + 1,
                "is_result": True,
                "datetime": "2021-12-26 14:00:00",
                "home_team": "Home United",
                "away_team": "Away Town",
                "home_goals": 0,
                "away_goals": 0,
                "home_xg": 0.5,
                "away_xg": 0.4,
            },
        ]
        write_parquet(pl.DataFrame(rows), out_dir / "part.parquet")
        _write_understat_player_match(data_root)

        with pytest.raises(ValueError, match="duplicate"):
            build_player_fixture_facts(SEASON, data_root=data_root)
