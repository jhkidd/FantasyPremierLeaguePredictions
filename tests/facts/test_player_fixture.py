from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fpl.config import Season
from fpl.facts.player_fixture import KEY, build_player_fixture_facts, write_player_fixture_facts
from fpl.staging.pipeline import stage_vaastav_source
from fpl.storage.raw_io import RawArtifact, write_raw

SEASON = Season(2025)

_MERGED_GW_HEADER = (
    "name,position,team,xP,assists,bonus,bps,clean_sheets,creativity,element,"
    "expected_assists,expected_goal_involvements,expected_goals,expected_goals_conceded,"
    "fixture,goals_conceded,goals_scored,ict_index,influence,kickoff_time,minutes,modified,"
    "opponent_team,own_goals,penalties_missed,penalties_saved,red_cards,round,saves,selected,"
    "starts,team_a_score,team_h_score,threat,total_points,transfers_balance,transfers_in,"
    "transfers_out,value,was_home,yellow_cards,clearances_blocks_interceptions,"
    "defensive_contribution,recoveries,tackles,GW\n"
)
_DEFENDER_ROW = (
    "Reinildo Mandava,DEF,Sunderland,0.5,0,0,27,1,2.5,541,0.00,0.00,0.00,0.56,5,0,0,1.6,13.6,"
    "2025-08-16T14:00:00Z,90,False,19,0,0,0,0,1,0,677026,1,0,3,0.0,6,0,0,0,40,True,0,6,8,3,2,1\n"
)
_BENCH_ROW = (
    "Lewis Dobbin,MID,Aston Villa,1.0,0,0,0,0,0.0,57,0.00,0.00,0.00,0.00,2,0,0,0.0,0.0,"
    "2025-08-16T11:30:00Z,0,False,15,0,0,0,0,1,0,4994,0,0,0,0.0,0,0,0,0,50,True,0,0,0,0,0,1\n"
)


def _write_merged_gw(data_root: Path, *rows: str) -> None:
    body = (_MERGED_GW_HEADER + "".join(rows)).encode("utf-8")
    artifact = RawArtifact(
        source="vaastav",
        endpoint="merged_gw",
        season=SEASON,
        url="https://github.com/vaastav/Fantasy-Premier-League/.../gws/merged_gw.csv",
        http_status=200,
        body=body,
        fetched_at=datetime(2026, 7, 31, tzinfo=UTC),
        connector_version="1",
        content_type="csv",
    )
    write_raw(artifact, data_root=data_root)


class TestBuildPlayerFixtureFacts:
    def test_no_staged_data_returns_none(self, tmp_path: Path) -> None:
        assert build_player_fixture_facts(SEASON, data_root=tmp_path / "data") is None

    def test_builds_one_row_per_staged_player_fixture(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, _DEFENDER_ROW, _BENCH_ROW)
        stage_vaastav_source(SEASON, data_root=data_root)

        facts = build_player_fixture_facts(SEASON, data_root=data_root)
        assert facts.height == 2
        assert facts.select(list(KEY)).is_duplicated().sum() == 0

    def test_defensive_columns_preserved(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, _DEFENDER_ROW)
        stage_vaastav_source(SEASON, data_root=data_root)

        facts = build_player_fixture_facts(SEASON, data_root=data_root)
        row = facts.row(0, named=True)
        assert row["cbi"] == 6
        assert row["tackles"] == 2
        assert row["recoveries"] == 3
        assert row["defensive_contribution"] == 8
        assert row["obs_defensive"] is True

    def test_bps_inputs_are_null_and_unobserved_for_e7(self, tmp_path: Path) -> None:
        """Finding 2: BPS inputs are only ever observed for 2016/17-2018/19.
        For 2025/26 (E7) they must be null with the mask false, never zero."""
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, _DEFENDER_ROW)
        stage_vaastav_source(SEASON, data_root=data_root)

        facts = build_player_fixture_facts(SEASON, data_root=data_root)
        row = facts.row(0, named=True)
        assert row["key_passes"] is None
        assert row["obs_bps_inputs"] is False

    def test_expected_columns_observed_for_e7(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, _DEFENDER_ROW)
        stage_vaastav_source(SEASON, data_root=data_root)

        facts = build_player_fixture_facts(SEASON, data_root=data_root)
        assert facts.row(0, named=True)["obs_expected"] is True

    def test_observed_fpl_columns_carry_the_fpl_suffix(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, _DEFENDER_ROW)
        stage_vaastav_source(SEASON, data_root=data_root)

        facts = build_player_fixture_facts(SEASON, data_root=data_root)
        assert "total_points_fpl" in facts.columns
        assert "total_points" not in facts.columns

    def test_no_row_with_zero_minutes_and_positive_points(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, _DEFENDER_ROW, _BENCH_ROW)
        stage_vaastav_source(SEASON, data_root=data_root)

        facts = build_player_fixture_facts(SEASON, data_root=data_root)
        import polars as pl

        violations = facts.filter((pl.col("minutes") == 0) & (pl.col("total_points_fpl") > 0))
        assert violations.height == 0

    def test_duplicate_key_raises(self, tmp_path: Path) -> None:
        """A genuine key collision (not the archive's exact-duplicate rows,
        which staging already removes) must surface loudly rather than
        silently overwrite one row with another."""
        import polars as pl

        from fpl.storage import paths as storage_paths
        from fpl.storage.parquet_io import write_parquet

        data_root = tmp_path / "data"
        duplicated = pl.DataFrame(
            {
                "season": ["2025-26", "2025-26"],
                "player_name": ["A", "A"],
                "position": ["DEF", "DEF"],
                "team": ["Sunderland", "Sunderland"],
                "player_id": [541, 541],
                "fixture_id": [1, 1],
                "event": [1, 1],
                "kickoff_time": ["2025-08-16T14:00:00Z", "2025-08-16T14:00:00Z"],
                "was_home": [True, True],
                "opponent_team": [19, 19],
                "minutes": [90, 45],
                "starts": [1, 1],
                "goals_scored": [0, 0],
                "assists": [0, 0],
                "goals_conceded": [0, 0],
                "own_goals": [0, 0],
                "penalties_saved": [0, 0],
                "penalties_missed": [0, 0],
                "yellow_cards": [0, 0],
                "red_cards": [0, 0],
                "saves": [0, 0],
                "bonus_fpl": [0, 0],
                "bps_fpl": [27, 27],
                "total_points_fpl": [2, 2],
                "clearances_blocks_interceptions": [6, 6],
                "tackles": [2, 2],
                "recoveries": [3, 3],
                "defensive_contribution": [8, 8],
            }
        )
        out_dir = storage_paths.staged_table("player_fixture_stats", SEASON, data_root=data_root)
        out_dir.mkdir(parents=True, exist_ok=True)
        write_parquet(duplicated, out_dir / "part.parquet")

        with pytest.raises(ValueError, match="not unique"):
            build_player_fixture_facts(SEASON, data_root=data_root)


class TestWritePlayerFixtureFacts:
    def test_writes_parquet_and_reports_row_count(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, _DEFENDER_ROW, _BENCH_ROW)
        stage_vaastav_source(SEASON, data_root=data_root)

        result = write_player_fixture_facts(SEASON, data_root=data_root)
        assert result.written
        assert result.frame.height == 2
        out_path = data_root / "facts" / "player_fixture" / "season=2025-26" / "part.parquet"
        assert out_path.exists()

    def test_rebuild_is_byte_identical(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, _DEFENDER_ROW, _BENCH_ROW)
        stage_vaastav_source(SEASON, data_root=data_root)
        out_path = data_root / "facts" / "player_fixture" / "season=2025-26" / "part.parquet"

        write_player_fixture_facts(SEASON, data_root=data_root)
        first = out_path.read_bytes()
        write_player_fixture_facts(SEASON, data_root=data_root)
        second = out_path.read_bytes()
        assert first == second

    def test_no_staged_data_reports_not_written(self, tmp_path: Path) -> None:
        result = write_player_fixture_facts(SEASON, data_root=tmp_path / "data")
        assert not result.written
