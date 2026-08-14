from __future__ import annotations

import pytest

from fpl.config import Season
from fpl.staging.vaastav import ERA_BY_SEASON, era_for_season, stage_merged_gw

# A trimmed, real excerpt of vaastav's 2025/26 gws/merged_gw.csv (fetched
# 2026-07-31), header plus a handful of representative rows: a defender with
# a qualifying defensive contribution, a zero-minutes bench player, and a
# synthetic manager-asset row appended to exercise Finding 6's exclusion.
_HEADER = (
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

_MANAGER_ROW = (
    "Pep Guardiola,AM,Man City,0.0,0,0,0,0,0.0,999,0.00,0.00,0.00,0.00,9,0,0,0.0,0.0,"
    "2025-08-16T14:00:00Z,0,False,1,0,0,0,0,1,0,0,0,0,0,0.0,12,0,0,0,0,True,0,0,0,0,0,1\n"
)


def _body(*rows: str) -> bytes:
    return (_HEADER + "".join(rows)).encode("utf-8")


SEASON = Season(2025)


class TestEraForSeason:
    def test_2025_26_is_e7(self) -> None:
        assert era_for_season(SEASON) == "E7"

    def test_unclassified_season_raises(self) -> None:
        with pytest.raises(ValueError, match="classify"):
            era_for_season(Season(2030))

    def test_era_map_covers_all_ten_backfilled_seasons(self) -> None:
        """Phase 6 adds E1-E6; all ten seasons from 2016/17 are classified."""
        assert set(ERA_BY_SEASON.values()) == {"E1", "E2", "E3", "E4", "E5", "E6", "E7"}
        assert len(ERA_BY_SEASON) == 10


class TestStageMergedGw:
    def test_stages_rows(self) -> None:
        result = stage_merged_gw(_body(_DEFENDER_ROW, _BENCH_ROW), SEASON)
        assert result.frame.height == 2
        assert result.excluded_manager_rows == 0

    def test_defensive_contribution_matches_its_definition_for_a_defender(self) -> None:
        """Finding 8: CBI + tackles (recoveries excluded) for a defender."""
        result = stage_merged_gw(_body(_DEFENDER_ROW), SEASON)
        row = result.frame.row(0, named=True)
        assert row["clearances_blocks_interceptions"] == 6
        assert row["tackles"] == 2
        assert row["recoveries"] == 3
        assert row["defensive_contribution"] == 8
        assert (
            row["clearances_blocks_interceptions"] + row["tackles"] == row["defensive_contribution"]
        )

    def test_manager_asset_rows_are_excluded_and_counted(self) -> None:
        result = stage_merged_gw(_body(_DEFENDER_ROW, _MANAGER_ROW), SEASON)
        assert result.frame.height == 1
        assert result.excluded_manager_rows == 1
        assert 999 not in result.frame["player_id"].to_list()

    def test_season_column_present(self) -> None:
        result = stage_merged_gw(_body(_DEFENDER_ROW), SEASON)
        assert result.frame["season"].unique().to_list() == ["2025-26"]

    def test_key_is_unique(self) -> None:
        result = stage_merged_gw(_body(_DEFENDER_ROW, _BENCH_ROW), SEASON)
        assert result.frame.select(["player_id", "fixture_id"]).is_duplicated().sum() == 0

    def test_drop_list_columns_are_never_staged(self) -> None:
        result = stage_merged_gw(_body(_DEFENDER_ROW), SEASON)
        for column in ("xP", "value", "selected", "transfers_in"):
            assert column not in result.frame.columns
        assert column not in result.report.unknown_columns

    def test_unclassified_season_raises_before_parsing(self) -> None:
        with pytest.raises(ValueError, match="classify"):
            stage_merged_gw(_body(_DEFENDER_ROW), Season(2030))
