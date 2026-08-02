from __future__ import annotations

from datetime import date

from fpl.config import Season
from fpl.staging.clubelo import RATINGS_SPEC, stage_ratings

AS_OF = date(2026, 8, 15)
SEASON = Season(2026)

RATINGS_CSV = (
    b"Rank,Club,Country,Level,Elo,From,To\n"
    b"1,Arsenal,ENG,1,2063.7578125,2026-05-31,2026-08-21\n"
    b"2,Man City,ENG,1,2029.451171875,2026-05-31,2026-08-21\n"
    b"45,Real Madrid,ESP,1,1998.1,2026-05-31,2026-08-21\n"
)


class TestStageRatings:
    def test_stamps_season_and_as_of_date_onto_every_row(self) -> None:
        result = stage_ratings(RATINGS_CSV, AS_OF, SEASON)
        assert result.frame["season"].to_list() == ["2026-27", "2026-27", "2026-27"]
        assert result.frame["as_of_date"].to_list() == [AS_OF.isoformat()] * 3

    def test_renames_and_types_every_declared_column(self) -> None:
        result = stage_ratings(RATINGS_CSV, AS_OF, SEASON)
        row = result.frame.row(0, named=True)
        assert row["rank"] == 1
        assert row["club"] == "Arsenal"
        assert row["country"] == "ENG"
        assert row["level"] == 1
        assert row["elo"] == 2063.7578125
        assert row["valid_from"] == "2026-05-31"
        assert row["valid_to"] == "2026-08-21"

    def test_non_english_clubs_are_kept_not_filtered(self) -> None:
        """Filtering to Premier League opponents is facts-assembly's job
        (plan §7.7), not staging's - the staged table stays a faithful copy
        of everything Club Elo published for that date."""
        result = stage_ratings(RATINGS_CSV, AS_OF, SEASON)
        assert "Real Madrid" in result.frame["club"].to_list()

    def test_report_row_counts_match_input(self) -> None:
        result = stage_ratings(RATINGS_CSV, AS_OF, SEASON)
        assert result.report.rows_in == 3
        assert result.report.rows_out == 3
        assert result.report.table == RATINGS_SPEC.table
