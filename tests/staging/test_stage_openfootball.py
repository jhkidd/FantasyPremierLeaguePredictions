from __future__ import annotations

from datetime import date

from fpl.config import Season
from fpl.staging.openfootball import FIXTURES_SPEC, stage_fixtures

SEASON = Season(2025)

CL_EXCERPT = """\
▪ League, Matchday 1
  Tue Sep 16 2025
    18:45  Athletic Club (ESP)     v Arsenal FC (ENG)         0-2 (0-0)
           PSV (NED)               v Royale Union Saint-Gilloise (BEL)  1-3 (0-2)
""".encode()


class TestStageFixtures:
    def test_stamps_season_and_competition_onto_every_row(self) -> None:
        result = stage_fixtures(CL_EXCERPT, SEASON, "champions_league")
        assert result.frame["season"].to_list() == ["2025-26", "2025-26"]
        assert result.frame["competition"].to_list() == ["champions_league", "champions_league"]

    def test_types_and_names_every_declared_column(self) -> None:
        result = stage_fixtures(CL_EXCERPT, SEASON, "champions_league")
        row = result.frame.row(0, named=True)
        assert row["match_date"] == date(2025, 9, 16)
        assert row["round"] == "League, Matchday 1"
        assert row["home_team"] == "Athletic Club"
        assert row["home_country"] == "ESP"
        assert row["away_team"] == "Arsenal FC"
        assert row["away_country"] == "ENG"

    def test_a_document_with_no_fixtures_produces_an_empty_not_missing_table(self) -> None:
        """A season where no tracked club reached, say, the Conference
        League qualifying rounds is a legitimate empty result, not an
        error - facts assembly can distinguish "zero rows" from "the
        connector failed" cleanly this way."""
        result = stage_fixtures(b"", SEASON, "conference_league_qualifying")
        assert result.frame.height == 0
        expected_columns = {"season", *(c.name for c in FIXTURES_SPEC.columns)}
        assert set(result.frame.columns) == expected_columns

    def test_report_row_counts_match_input(self) -> None:
        result = stage_fixtures(CL_EXCERPT, SEASON, "champions_league")
        assert result.report.rows_in == 2
        assert result.report.rows_out == 2
        assert result.report.table == FIXTURES_SPEC.table
