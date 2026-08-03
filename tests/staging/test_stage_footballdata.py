from __future__ import annotations

from fpl.config import Season
from fpl.staging.footballdata import MATCHES_AND_ODDS_SPEC, stage_matches_and_odds

SEASON = Season(2025)

# Trimmed excerpt of the live mmz4281/2526/E0.csv, confirmed during phase 7
# probing: real header (with a sample of undeclared bookmaker columns kept
# in, to exercise the unknown-column warning path), two real rows.
MATCH_CSV = (
    b"Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HTHG,HTAG,HTR,Referee,"
    b"B365H,B365D,B365A,BFDH,BFDD,BFDA\n"
    b"E0,15/08/2025,20:00,Liverpool,Bournemouth,4,2,H,1,0,H,A Taylor,"
    b"1.3,6,8.5,1.3,6,9.5\n"
    b"E0,16/08/2025,12:30,Aston Villa,Newcastle,0,0,D,0,0,D,C Pawson,"
    b"2.25,3.5,2.9,2.25,3.75,3.1\n"
)


class TestStageMatchesAndOdds:
    def test_stamps_season_onto_every_row(self) -> None:
        result = stage_matches_and_odds(MATCH_CSV, SEASON)
        assert result.frame["season"].to_list() == ["2025-26", "2025-26"]

    def test_renames_and_types_every_declared_column(self) -> None:
        result = stage_matches_and_odds(MATCH_CSV, SEASON)
        row = result.frame.row(0, named=True)
        assert row["match_date"] == "15/08/2025"
        assert row["home_team"] == "Liverpool"
        assert row["away_team"] == "Bournemouth"
        assert row["full_time_home_goals"] == 4
        assert row["full_time_away_goals"] == 2
        assert row["full_time_result"] == "H"
        assert row["bet365_home_odds"] == 1.3
        assert row["bet365_draw_odds"] == 6.0
        assert row["bet365_away_odds"] == 8.5

    def test_undeclared_bookmaker_columns_are_a_warning_not_a_failure(self) -> None:
        result = stage_matches_and_odds(MATCH_CSV, SEASON)
        assert "BFDH" in result.report.unknown_columns
        assert "BFDD" in result.report.unknown_columns
        assert "BFDA" in result.report.unknown_columns

    def test_raw_odds_are_kept_untouched_not_normalised(self) -> None:
        """Overround removal / implied-probability normalisation is
        facts-assembly's job (plan §7.12), not staging's - staging only
        types and selects what the source actually published."""
        result = stage_matches_and_odds(MATCH_CSV, SEASON)
        assert result.frame["bet365_home_odds"].to_list() == [1.3, 2.25]

    def test_report_row_counts_match_input(self) -> None:
        result = stage_matches_and_odds(MATCH_CSV, SEASON)
        assert result.report.rows_in == 2
        assert result.report.rows_out == 2
        assert result.report.table == MATCHES_AND_ODDS_SPEC.table
