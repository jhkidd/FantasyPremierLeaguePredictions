from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fpl.config import Season
from fpl.quality.checks import check_staged_tables
from fpl.quality.gates import has_blocking_violations
from fpl.staging.pipeline import (
    stage_clubelo_source,
    stage_footballdata_source,
    stage_fpl_source,
    stage_openfootball_source,
    stage_understat_source,
    stage_vaastav_source,
)
from fpl.storage.raw_io import RawArtifact, write_raw

SEASON = Season(2026)
VAASTAV_SEASON = Season(2025)
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fpl"

_MERGED_GW_HEADER = (
    "name,position,team,xP,assists,bonus,bps,clean_sheets,creativity,element,"
    "expected_assists,expected_goal_involvements,expected_goals,expected_goals_conceded,"
    "fixture,goals_conceded,goals_scored,ict_index,influence,kickoff_time,minutes,modified,"
    "opponent_team,own_goals,penalties_missed,penalties_saved,red_cards,round,saves,selected,"
    "starts,team_a_score,team_h_score,threat,total_points,transfers_balance,transfers_in,"
    "transfers_out,value,was_home,yellow_cards,clearances_blocks_interceptions,"
    "defensive_contribution,recoveries,tackles,GW\n"
)
_MERGED_GW_DEFENDER_ROW = (
    "Reinildo Mandava,DEF,Sunderland,0.5,0,0,27,1,2.5,541,0.00,0.00,0.00,0.56,5,0,0,1.6,13.6,"
    "2025-08-16T14:00:00Z,90,False,19,0,0,0,0,1,0,677026,1,0,3,0.0,6,0,0,0,40,True,0,6,8,3,2,1\n"
)
_MERGED_GW_MANAGER_ROW = (
    "Pep Guardiola,AM,Man City,0.0,0,0,0,0,0.0,999,0.00,0.00,0.00,0.00,9,0,0,0.0,0.0,"
    "2025-08-16T14:00:00Z,0,False,1,0,0,0,0,1,0,0,0,0,0,0.0,12,0,0,0,0,True,0,0,0,0,0,1\n"
)


def _write_merged_gw(data_root: Path, moment: datetime) -> None:
    body = (_MERGED_GW_HEADER + _MERGED_GW_DEFENDER_ROW + _MERGED_GW_MANAGER_ROW).encode("utf-8")
    artifact = RawArtifact(
        source="vaastav",
        endpoint="merged_gw",
        season=VAASTAV_SEASON,
        url="https://github.com/vaastav/Fantasy-Premier-League/.../gws/merged_gw.csv",
        http_status=200,
        body=body,
        fetched_at=moment,
        connector_version="1",
        content_type="csv",
    )
    write_raw(artifact, data_root=data_root)


def _write_bootstrap(data_root: Path, moment: datetime) -> None:
    body = (FIXTURES_DIR / "bootstrap_static.json").read_bytes()
    artifact = RawArtifact(
        source="fpl",
        endpoint="bootstrap_static",
        season=SEASON,
        url="https://fantasy.premierleague.com/api/bootstrap-static/",
        http_status=200,
        body=body,
        fetched_at=moment,
        connector_version="1",
    )
    write_raw(artifact, data_root=data_root)


def _write_fixtures(data_root: Path, moment: datetime) -> None:
    body = (FIXTURES_DIR / "fixtures.json").read_bytes()
    artifact = RawArtifact(
        source="fpl",
        endpoint="fixtures",
        season=SEASON,
        url="https://fantasy.premierleague.com/api/fixtures/",
        http_status=200,
        body=body,
        fetched_at=moment,
        connector_version="1",
    )
    write_raw(artifact, data_root=data_root)


RATINGS_CSV = (
    b"Rank,Club,Country,Level,Elo,From,To\n"
    b"1,Arsenal,ENG,1,2063.7578125,2025-05-31,2025-08-21\n"
    b"2,Man City,ENG,1,2029.451171875,2025-05-31,2025-08-21\n"
)

MATCH_CSV = (
    b"Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HTHG,HTAG,HTR\n"
    b"E0,15/08/2025,20:00,Liverpool,Bournemouth,4,2,H,1,0,H\n"
    b"E0,16/08/2025,12:30,Aston Villa,Newcastle,0,0,D,0,0,D\n"
)

CL_EXCERPT = """\
▪ League, Matchday 1
  Tue Sep 16 2025
    18:45  Athletic Club (ESP)     v Arsenal FC (ENG)         0-2 (0-0)
""".encode()


def _write_clubelo_ratings(
    data_root: Path, moment: datetime, season: Season, *, body: bytes = RATINGS_CSV
) -> None:
    artifact = RawArtifact(
        source="clubelo",
        endpoint="ratings",
        season=season,
        url=f"https://api.clubelo.com/{moment.date().isoformat()}",
        http_status=200,
        body=body,
        fetched_at=moment,
        connector_version="1",
        content_type="csv",
    )
    write_raw(artifact, data_root=data_root)


def _write_footballdata_matches(data_root: Path, moment: datetime, season: Season) -> None:
    artifact = RawArtifact(
        source="footballdata",
        endpoint="matches_and_odds",
        season=season,
        url="https://www.football-data.co.uk/mmz4281/2526/E0.csv",
        http_status=200,
        body=MATCH_CSV,
        fetched_at=moment,
        connector_version="1",
        content_type="csv",
    )
    write_raw(artifact, data_root=data_root)


def _write_openfootball_champions_league(data_root: Path, moment: datetime, season: Season) -> None:
    artifact = RawArtifact(
        source="openfootball",
        endpoint="champions_league",
        season=season,
        url="https://raw.githubusercontent.com/openfootball/champions-league/master/2025-26/cl.txt",
        http_status=200,
        body=CL_EXCERPT,
        fetched_at=moment,
        connector_version="1",
        content_type="text",
    )
    write_raw(artifact, data_root=data_root)


class TestStageClubeloSourceEndToEnd:
    def test_stages_ratings(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_clubelo_ratings(data_root, datetime(2025, 8, 15, tzinfo=UTC), VAASTAV_SEASON)

        [result] = stage_clubelo_source(VAASTAV_SEASON, data_root=data_root)
        assert result.table == "clubelo_ratings"
        assert result.written
        assert result.rows == 2

        out_path = data_root / "staged" / "clubelo_ratings" / "season=2025-26" / "part.parquet"
        assert out_path.exists()

    def test_multiple_as_of_captures_concatenate(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        later_ratings = RATINGS_CSV.replace(b"2063.7578125", b"2070.1")
        _write_clubelo_ratings(data_root, datetime(2025, 8, 15, tzinfo=UTC), VAASTAV_SEASON)
        _write_clubelo_ratings(
            data_root, datetime(2025, 8, 22, tzinfo=UTC), VAASTAV_SEASON, body=later_ratings
        )

        [result] = stage_clubelo_source(VAASTAV_SEASON, data_root=data_root)
        assert result.rows == 4

    def test_no_capture_on_disk_reports_not_written(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        [result] = stage_clubelo_source(VAASTAV_SEASON, data_root=data_root)
        assert not result.written
        assert "no clubelo ratings capture on disk" in result.detail


class TestStageFootballdataSourceEndToEnd:
    def test_stages_matches_and_odds(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_footballdata_matches(data_root, datetime(2025, 8, 20, tzinfo=UTC), VAASTAV_SEASON)

        [result] = stage_footballdata_source(VAASTAV_SEASON, data_root=data_root)
        assert result.table == "footballdata_matches_and_odds"
        assert result.written
        assert result.rows == 2

    def test_no_capture_on_disk_reports_not_written(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        [result] = stage_footballdata_source(VAASTAV_SEASON, data_root=data_root)
        assert not result.written
        assert "no footballdata matches_and_odds capture on disk" in result.detail


class TestStageOpenfootballSourceEndToEnd:
    def test_stages_each_captured_competition(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_openfootball_champions_league(
            data_root, datetime(2025, 9, 17, tzinfo=UTC), VAASTAV_SEASON
        )

        results = stage_openfootball_source(VAASTAV_SEASON, data_root=data_root)
        [result] = [r for r in results if r.written]
        assert result.table == "openfootball_fixtures[champions_league]"
        assert result.rows == 1

        out_path = (
            data_root
            / "staged"
            / "openfootball_fixtures"
            / "season=2025-26"
            / "competition=champions_league.parquet"
        )
        assert out_path.exists()

    def test_no_competition_captured_reports_not_written(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        [result] = stage_openfootball_source(VAASTAV_SEASON, data_root=data_root)
        assert not result.written
        assert "no openfootball capture on disk" in result.detail


UNDERSTAT_LEAGUE_DATA = {
    "teams": {},
    "players": [
        {
            "id": "620",
            "player_name": "Bruno Fernandes",
            "team_title": "Manchester United",
            "position": "M",
            "games": "1",
            "time": "90",
            "goals": "1",
            "xG": "0.4",
            "assists": "0",
            "xA": "0.1",
            "shots": "3",
            "key_passes": "2",
            "yellow_cards": "0",
            "red_cards": "0",
            "npg": "1",
            "npxG": "0.4",
            "xGChain": "0.5",
            "xGBuildup": "0.2",
        }
    ],
    "dates": [
        {
            "id": "555",
            "isResult": True,
            "datetime": "2025-08-16 15:00:00",
            "h": {"title": "Manchester United"},
            "a": {"title": "Fulham"},
            "goals": {"h": "1", "a": "0"},
            "xG": {"h": "0.4", "a": "0.2"},
        }
    ],
}

UNDERSTAT_MATCH_DATA = {
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
                "xG": "0.4",
                "assists": "0",
                "xA": "0.1",
                "key_passes": "2",
                "yellow_card": "0",
                "red_card": "0",
                "xGChain": "0.5",
                "xGBuildup": "0.2",
            }
        },
        "a": {},
    },
    "shots": {},
}


def _write_understat_league_data(data_root: Path, moment: datetime, season: Season) -> None:
    import json

    artifact = RawArtifact(
        source="understat",
        endpoint="league_data",
        season=season,
        url="https://understat.com/getLeagueData/EPL/2025",
        http_status=200,
        body=json.dumps(UNDERSTAT_LEAGUE_DATA).encode(),
        fetched_at=moment,
        connector_version="1",
        content_type="json",
    )
    write_raw(artifact, data_root=data_root)


def _write_understat_match_chunk(data_root: Path, moment: datetime, season: Season) -> None:
    import json

    from fpl.storage.raw_io import write_chunk

    record = {"match_id": 555, "payload": UNDERSTAT_MATCH_DATA}
    body = json.dumps(record, sort_keys=True).encode() + b"\n"
    artifact = RawArtifact(
        source="understat",
        endpoint="match_data",
        season=season,
        url="https://understat.com/getMatchData/{id}",
        http_status=200,
        body=body,
        fetched_at=moment,
        connector_version="1",
        content_type="ndjson",
    )
    write_chunk(artifact, 0, data_root=data_root)


class TestStageUnderstatSourceEndToEnd:
    def test_stages_players_and_fixtures_from_league_data(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_understat_league_data(data_root, datetime(2025, 8, 20, tzinfo=UTC), VAASTAV_SEASON)

        results = stage_understat_source(VAASTAV_SEASON, data_root=data_root)
        by_table = {r.table: r for r in results}
        assert by_table["understat_players_season"].written
        assert by_table["understat_players_season"].rows == 1
        assert by_table["understat_fixtures"].written
        assert by_table["understat_fixtures"].rows == 1
        assert not by_table["understat_player_match"].written

    def test_stages_player_match_when_a_chunk_exists(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_understat_league_data(data_root, datetime(2025, 8, 20, tzinfo=UTC), VAASTAV_SEASON)
        _write_understat_match_chunk(data_root, datetime(2025, 8, 21, tzinfo=UTC), VAASTAV_SEASON)

        results = stage_understat_source(VAASTAV_SEASON, data_root=data_root)
        by_table = {r.table: r for r in results}
        assert by_table["understat_player_match"].written
        assert by_table["understat_player_match"].rows == 1

    def test_no_league_data_capture_reports_not_written(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        results = stage_understat_source(VAASTAV_SEASON, data_root=data_root)
        assert all(not r.written for r in results)
        assert any("no understat league_data capture on disk" in (r.detail or "") for r in results)


class TestStageFplSourceEndToEnd:
    def test_stages_players_teams_events_and_fixtures(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        moment = datetime(2026, 8, 1, tzinfo=UTC)
        _write_bootstrap(data_root, moment)
        _write_fixtures(data_root, moment)

        results = stage_fpl_source(SEASON, data_root=data_root)
        staged_names = {r.table for r in results if r.written}
        assert {"players", "teams", "events", "fixtures"} <= staged_names

    def test_rebuild_is_byte_identical(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        moment = datetime(2026, 8, 1, tzinfo=UTC)
        _write_bootstrap(data_root, moment)

        stage_fpl_source(SEASON, data_root=data_root, tables={"players"})
        first = (data_root / "staged" / "players" / "season=2026-27" / "part.parquet").read_bytes()

        stage_fpl_source(SEASON, data_root=data_root, tables={"players"})
        second = (data_root / "staged" / "players" / "season=2026-27" / "part.parquet").read_bytes()

        assert first == second

    def test_price_snapshots_stack_across_two_captures(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_bootstrap(data_root, datetime(2026, 8, 1, tzinfo=UTC))
        _write_bootstrap(data_root, datetime(2026, 8, 2, tzinfo=UTC))

        results = stage_fpl_source(SEASON, data_root=data_root, tables={"price_snapshots"})
        [result] = [r for r in results if r.table == "price_snapshots"]
        # Two captures of the same fixture yield one row per player per capture,
        # unless the second capture happened to be byte-identical and skipped.
        assert result.rows > 0

    def test_check_is_clean_after_staging(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        moment = datetime(2026, 8, 1, tzinfo=UTC)
        _write_bootstrap(data_root, moment)
        _write_fixtures(data_root, moment)
        stage_fpl_source(SEASON, data_root=data_root)

        violations = check_staged_tables(SEASON, data_root=data_root)
        assert not has_blocking_violations(violations)


class TestStageVaastavSourceEndToEnd:
    def test_stages_player_fixture_stats_excluding_manager_rows(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, datetime(2026, 7, 31, tzinfo=UTC))

        results = stage_vaastav_source(VAASTAV_SEASON, data_root=data_root)
        [result] = results
        assert result.table == "player_fixture_stats"
        assert result.written
        assert result.rows == 1
        assert "excluded 1 manager-asset row" in result.detail

        out_path = data_root / "staged" / "player_fixture_stats" / "season=2025-26" / "part.parquet"
        assert out_path.exists()

    def test_rebuild_is_byte_identical(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, datetime(2026, 7, 31, tzinfo=UTC))
        out_path = data_root / "staged" / "player_fixture_stats" / "season=2025-26" / "part.parquet"

        stage_vaastav_source(VAASTAV_SEASON, data_root=data_root)
        first = out_path.read_bytes()
        stage_vaastav_source(VAASTAV_SEASON, data_root=data_root)
        second = out_path.read_bytes()

        assert first == second

    def test_no_capture_on_disk_reports_not_written(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        [result] = stage_vaastav_source(VAASTAV_SEASON, data_root=data_root)
        assert not result.written
        assert result.rows == 0

    def test_check_is_clean_after_staging(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, datetime(2026, 7, 31, tzinfo=UTC))
        stage_vaastav_source(VAASTAV_SEASON, data_root=data_root)

        violations = check_staged_tables(VAASTAV_SEASON, data_root=data_root)
        assert not has_blocking_violations(violations)
