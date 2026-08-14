from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fpl.config import Season
from fpl.facts.points import build_points, ruleset_for_name, write_points
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


class TestRulesetForName:
    def test_known_names_resolve(self) -> None:
        for name in ("legacy", "2025-26", "2026-27"):
            assert ruleset_for_name(name).name == name

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown ruleset"):
            ruleset_for_name("2099-00")


class TestBuildPoints:
    def test_no_facts_returns_none(self, tmp_path: Path) -> None:
        assert build_points(SEASON, "2025-26", data_root=tmp_path / "data") is None

    def test_scores_every_row_and_matches_fpl_at_zero_tolerance(self, tmp_path: Path) -> None:
        """The reconciliation milestone, on the real trimmed 2025/26 rows
        already used elsewhere as golden fixtures (Reinildo Mandava's
        defensive contribution, and a zero-minute bench player)."""
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, _DEFENDER_ROW, _BENCH_ROW)
        stage_vaastav_source(SEASON, data_root=data_root)

        points = build_points(SEASON, "2025-26", data_root=data_root)
        assert points.height == 2
        mismatches = points.filter(points["total"] != points["total_points_fpl"])
        assert mismatches.height == 0, mismatches

    def test_defender_dc_term_matches_fpl_bps_independent_total(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, _DEFENDER_ROW)
        stage_vaastav_source(SEASON, data_root=data_root)

        points = build_points(SEASON, "2025-26", data_root=data_root)
        row = points.row(0, named=True)
        # 6 CBI + 2 tackles = 8, below the defender threshold of 10 — no DC
        # points, even though the raw defensive_contribution *count* is 8.
        assert row["defensive_contribution"] == 0
        assert row["total"] == row["total_points_fpl"]


class TestWritePoints:
    def test_writes_parquet_under_the_named_ruleset(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_merged_gw(data_root, _DEFENDER_ROW)
        stage_vaastav_source(SEASON, data_root=data_root)

        result = write_points(SEASON, "2025-26", data_root=data_root)
        assert result.written
        out_path = (
            data_root / "facts" / "points" / "rules=2025-26" / "season=2025-26" / "part.parquet"
        )
        assert out_path.exists()

    def test_no_facts_reports_not_written(self, tmp_path: Path) -> None:
        result = write_points(SEASON, "2025-26", data_root=tmp_path / "data")
        assert not result.written
