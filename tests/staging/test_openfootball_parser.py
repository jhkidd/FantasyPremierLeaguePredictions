from __future__ import annotations

from datetime import date

import pytest

from fpl.sources.errors import SchemaError
from fpl.staging.openfootball_parser import ParsedFixture, parse_football_txt

# Trimmed excerpt of the live openfootball/champions-league 2025-26 cl.txt,
# confirmed during phase 7 probing: real formatting, cut down to two
# matchdays plus the knockout final stage so round transitions, multi-match
# kickoff-time groups, and the no-year date continuation are all exercised.
CL_EXCERPT = """\
= UEFA Champions League 2025/26

# Date       Tue Sep 16 2025 - Sat May 30 2026 (256d)
# Teams      36
# Matches    189
# Stages     League (144)  Playoffs (16)  Finals (29)



▪ League, Matchday 1
  Tue Sep 16 2025
    18:45  Athletic Club (ESP)     v Arsenal FC (ENG)         0-2 (0-0)
           PSV (NED)               v Royale Union Saint-Gilloise (BEL)  1-3 (0-2)
    21:00  Juventus FC (ITA)       v Borussia Dortmund (GER)  4-4 (0-0)
  Wed Sep 17
    18:45  SK Slavia Praha (CZE)   v FK Bodø/Glimt (NOR)      2-2 (1-0)
           PAE Olympiakos SFP (GRE) v Paphos FC (CYP)          0-0


▪ League, Matchday 2
  Tue Sep 30
    18:45  Atalanta BC (ITA)       v Club Brugge KV (BEL)     2-1 (0-1)


▪ Finals, Final
  Sat May 30
    18:00  Paris Saint-Germain FC (FRA) v Arsenal FC (ENG)         4-3 pen. 1-1 a.e.t. (1-1, 0-1)
"""


class TestParseFootballTxt:
    def test_extracts_every_fixture_in_file_order(self) -> None:
        fixtures = parse_football_txt(CL_EXCERPT)
        assert len(fixtures) == 7
        assert fixtures[0] == ParsedFixture(
            match_date=date(2025, 9, 16),
            round="League, Matchday 1",
            home_team="Athletic Club",
            home_country="ESP",
            away_team="Arsenal FC",
            away_country="ENG",
        )

    def test_a_continuation_line_at_the_same_kickoff_time_gets_the_same_date(self) -> None:
        fixtures = parse_football_txt(CL_EXCERPT)
        psv = next(f for f in fixtures if f.home_team == "PSV")
        assert psv.match_date == date(2025, 9, 16)

    def test_a_date_header_without_a_year_carries_the_year_forward(self) -> None:
        fixtures = parse_football_txt(CL_EXCERPT)
        slavia = next(f for f in fixtures if f.home_team == "SK Slavia Praha")
        assert slavia.match_date == date(2025, 9, 17)

    def test_round_headers_apply_to_every_fixture_until_the_next_one(self) -> None:
        fixtures = parse_football_txt(CL_EXCERPT)
        matchday_2 = next(f for f in fixtures if f.home_team == "Atalanta BC")
        assert matchday_2.round == "League, Matchday 2"

    def test_a_multi_word_round_label_after_the_marker_is_kept_whole(self) -> None:
        fixtures = parse_football_txt(CL_EXCERPT)
        final = fixtures[-1]
        assert final.round == "Finals, Final"

    def test_a_new_explicit_year_overrides_the_carried_forward_one(self) -> None:
        fixtures = parse_football_txt(CL_EXCERPT)
        final = fixtures[-1]
        assert final.match_date == date(2026, 5, 30)

    def test_score_and_extra_time_annotations_are_ignored(self) -> None:
        """The final's score line carries `4-3 pen. 1-1 a.e.t. (1-1, 0-1)` —
        none of that should leak into the parsed team names."""
        fixtures = parse_football_txt(CL_EXCERPT)
        final = fixtures[-1]
        assert final.away_team == "Arsenal FC"
        assert final.away_country == "ENG"

    def test_unplayed_fixture_with_no_score_at_all_still_parses(self) -> None:
        text = (
            "▪ 1. Round\n"
            "  Tue Jul 8 2025\n"
            "    19:00  The New Saints (WAL)    v KF Shkëndija 79 (MKD)\n"
        )
        fixtures = parse_football_txt(text)
        assert len(fixtures) == 1
        assert fixtures[0].away_team == "KF Shkëndija 79"

    def test_a_fixture_before_any_date_header_is_a_schema_error(self) -> None:
        text = "▪ League, Matchday 1\n    18:45  Arsenal FC (ENG)  v  Chelsea FC (ENG)  1-0\n"
        with pytest.raises(SchemaError, match="before any date header"):
            parse_football_txt(text)

    def test_a_fixture_before_any_round_header_is_a_schema_error(self) -> None:
        text = "  Tue Sep 16 2025\n    18:45  Arsenal FC (ENG)  v  Chelsea FC (ENG)  1-0\n"
        with pytest.raises(SchemaError, match="before any round header"):
            parse_football_txt(text)

    def test_a_date_with_no_year_and_none_seen_yet_is_a_schema_error(self) -> None:
        with pytest.raises(SchemaError, match="no year"):
            parse_football_txt("▪ League, Matchday 1\n  Tue Sep 16\n")

    def test_december_to_january_rolls_the_year_forward(self) -> None:
        """Synthetic - the real file never crosses a December/January
        boundary within the trimmed excerpt above, but Champions League
        group-stage matchdays genuinely do (Dec fixtures, Jan continuation),
        so this edge case is exercised directly rather than left unverified."""
        text = (
            "▪ League, Matchday 6\n"
            "  Tue Dec 9 2025\n"
            "    21:00  Arsenal FC (ENG)  v  Chelsea FC (ENG)  1-0\n"
            "  Wed Jan 21\n"
            "    21:00  Chelsea FC (ENG)  v  Arsenal FC (ENG)  2-1\n"
        )
        fixtures = parse_football_txt(text)
        assert fixtures[0].match_date == date(2025, 12, 9)
        assert fixtures[1].match_date == date(2026, 1, 21)

    def test_empty_document_yields_no_fixtures(self) -> None:
        assert parse_football_txt("") == []
