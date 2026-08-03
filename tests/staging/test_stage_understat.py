from __future__ import annotations

import json

from fpl.config import Season
from fpl.staging.understat import (
    FIXTURES_SPEC,
    PLAYER_MATCH_SPEC,
    PLAYERS_SEASON_SPEC,
    stage_fixtures,
    stage_league_players,
    stage_match_data,
)

SEASON = Season(2025)

LEAGUE_DATA = {
    "teams": {"1": {"title": "Manchester United"}},
    "players": [
        {
            "id": "620",
            "player_name": "Bruno Fernandes",
            "team_title": "Manchester United",
            "position": "M",
            "games": "38",
            "time": "3200",
            "goals": "10",
            "xG": "8.5432",
            "assists": "8",
            "xA": "6.1234",
            "shots": "90",
            "key_passes": "70",
            "yellow_cards": "5",
            "red_cards": "0",
            "npg": "8",
            "npxG": "7.1",
            "xGChain": "10.2",
            "xGBuildup": "5.5",
        }
    ],
    "dates": [
        {
            "id": "1",
            "isResult": True,
            "datetime": "2025-08-16 15:00:00",
            "h": {"title": "Manchester United"},
            "a": {"title": "Fulham"},
            "goals": {"h": "1", "a": "0"},
            "xG": {"h": "1.4321", "a": "0.5"},
        },
        {
            "id": "2",
            "isResult": False,
            "datetime": "2026-05-24 15:00:00",
            "h": {"title": "Fulham"},
            "a": {"title": "Manchester United"},
        },
    ],
}

MATCH_DATA = {
    "rosters": {
        "h": {
            "3200": {
                "player": "Bruno Fernandes",
                "player_id": "620",
                "team_id": "89",
                "position": "M",
                "time": "90",
                "goals": "1",
                "own_goals": "0",
                "shots": "3",
                "xG": "0.4123",
                "assists": "0",
                "xA": "0.1",
                "key_passes": "2",
                "yellow_card": "0",
                "red_card": "0",
                "xGChain": "0.5",
                "xGBuildup": "0.2",
            }
        },
        "a": {
            "4100": {
                "player": "Someone Else",
                "player_id": "999",
                "team_id": "55",
                "position": "F",
                "time": "90",
                "goals": "0",
                "own_goals": "0",
                "shots": "1",
                "xG": "0.05",
                "assists": "0",
                "xA": "0.0",
                "key_passes": "0",
                "yellow_card": "1",
                "red_card": "0",
                "xGChain": "0.05",
                "xGBuildup": "0.0",
            }
        },
    },
    "shots": {},
}


class TestStageLeaguePlayers:
    def test_renames_and_types_every_declared_column(self) -> None:
        result = stage_league_players(json.dumps(LEAGUE_DATA).encode(), SEASON)
        row = result.frame.row(0, named=True)
        assert row["player_id"] == 620
        assert row["player_name"] == "Bruno Fernandes"
        assert row["team_title"] == "Manchester United"
        assert row["xg"] == 8.5432
        assert row["minutes"] == 3200
        assert row["season"] == "2025-26"

    def test_report_row_counts_match_input(self) -> None:
        result = stage_league_players(json.dumps(LEAGUE_DATA).encode(), SEASON)
        assert result.report.rows_in == 1
        assert result.report.rows_out == 1
        assert result.report.table == PLAYERS_SEASON_SPEC.table

    def test_empty_players_list_yields_an_empty_frame(self) -> None:
        result = stage_league_players(json.dumps({"players": []}).encode(), SEASON)
        assert result.frame.height == 0


class TestStageFixtures:
    def test_stages_both_played_and_unplayed_fixtures(self) -> None:
        result = stage_fixtures(json.dumps(LEAGUE_DATA).encode(), SEASON)
        assert result.frame.height == 2
        played = result.frame.filter(result.frame["is_result"]).row(0, named=True)
        assert played["home_team"] == "Manchester United"
        assert played["away_team"] == "Fulham"
        assert played["home_goals"] == 1
        assert played["home_xg"] == 1.4321

    def test_unplayed_fixture_has_null_score_and_xg(self) -> None:
        result = stage_fixtures(json.dumps(LEAGUE_DATA).encode(), SEASON)
        unplayed = result.frame.filter(~result.frame["is_result"]).row(0, named=True)
        assert unplayed["home_goals"] is None
        assert unplayed["home_xg"] is None

    def test_report_table_name(self) -> None:
        result = stage_fixtures(json.dumps(LEAGUE_DATA).encode(), SEASON)
        assert result.report.table == FIXTURES_SPEC.table


class TestStageMatchData:
    def test_stamps_match_id_and_side_onto_every_roster_row(self) -> None:
        result = stage_match_data(json.dumps(MATCH_DATA).encode(), 12345, SEASON)
        assert result.frame.height == 2
        assert set(result.frame["match_id"].to_list()) == {12345}
        assert set(result.frame["side"].to_list()) == {"h", "a"}

    def test_renames_and_types_columns(self) -> None:
        result = stage_match_data(json.dumps(MATCH_DATA).encode(), 12345, SEASON)
        home_row = result.frame.filter(result.frame["side"] == "h").row(0, named=True)
        assert home_row["player_id"] == 620
        assert home_row["player_name"] == "Bruno Fernandes"
        assert home_row["xg"] == 0.4123
        assert home_row["minutes"] == 90

    def test_report_table_name(self) -> None:
        result = stage_match_data(json.dumps(MATCH_DATA).encode(), 12345, SEASON)
        assert result.report.table == PLAYER_MATCH_SPEC.table
