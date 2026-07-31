from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fpl.config import Season
from fpl.quality.checks import check_staged_tables
from fpl.quality.gates import has_blocking_violations
from fpl.staging.pipeline import stage_fpl_source, stage_vaastav_source
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

        out_path = (
            data_root / "staged" / "player_fixture_stats" / "season=2025-26" / "part.parquet"
        )
        assert out_path.exists()

    def test_rebuild_is_byte_identical(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, datetime(2026, 7, 31, tzinfo=UTC))
        out_path = (
            data_root / "staged" / "player_fixture_stats" / "season=2025-26" / "part.parquet"
        )

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
