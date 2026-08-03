from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from fpl.config import Season
from fpl.identity.team_external_ids import (
    TEAM_EXTERNAL_ID_COLUMNS,
    collect_source_names,
    draft_team_external_ids,
    load_team_external_ids,
    refresh_team_external_ids,
    unmapped_source_names,
    write_team_external_ids,
)
from fpl.storage.raw_io import RawArtifact, write_raw

SEASON = Season(2025)

FPL_TEAMS = pl.DataFrame(
    {
        "team_code": ["3", "43", "90"],
        "canonical_name": ["Man Utd Placeholder", "Arsenal", "Bournemouth"],
    }
)
# team_code=3's canonical_name is deliberately awkward here - real FPL data
# spells it "Man Utd" in some seasons, "Manchester United" in others; the
# synonym table below is what closes that gap either way.
FPL_TEAMS_REAL = pl.DataFrame(
    {
        "team_code": ["3", "43", "90"],
        "canonical_name": ["Manchester United", "Arsenal", "Bournemouth"],
    }
)


def _write_raw_csv(
    data_root: Path, source: str, endpoint: str, season: Season, body: bytes
) -> None:
    artifact = RawArtifact(
        source=source,
        endpoint=endpoint,
        season=season,
        url=f"https://example.invalid/{source}/{endpoint}",
        http_status=200,
        body=body,
        fetched_at=datetime(2026, 8, 15, tzinfo=UTC),
        connector_version="1",
        content_type="csv",
    )
    write_raw(artifact, data_root=data_root)


def _write_raw_txt(
    data_root: Path, source: str, endpoint: str, season: Season, body: bytes
) -> None:
    artifact = RawArtifact(
        source=source,
        endpoint=endpoint,
        season=season,
        url=f"https://example.invalid/{source}/{endpoint}",
        http_status=200,
        body=body,
        fetched_at=datetime(2026, 8, 15, tzinfo=UTC),
        connector_version="1",
        content_type="txt",
    )
    write_raw(artifact, data_root=data_root)


class TestDraftTeamExternalIds:
    def test_every_fpl_team_gets_a_row_even_with_no_source_names(self) -> None:
        draft = draft_team_external_ids(FPL_TEAMS_REAL)
        assert draft.height == 3
        assert list(draft.columns) == list(TEAM_EXTERNAL_ID_COLUMNS)
        assert draft["clubelo_name"].to_list() == [None, None, None]

    def test_a_literal_name_match_is_drafted(self) -> None:
        draft = draft_team_external_ids(FPL_TEAMS_REAL, clubelo_names=["Arsenal", "Real Madrid"])
        row = draft.filter(pl.col("team_code") == "43").row(0, named=True)
        assert row["clubelo_name"] == "Arsenal"
        # Real Madrid shares no token with any FPL club - left unmatched.
        assert draft.filter(pl.col("clubelo_name") == "Real Madrid").height == 0

    def test_a_known_short_form_is_resolved_via_the_synonym_table(self) -> None:
        draft = draft_team_external_ids(FPL_TEAMS_REAL, footballdata_names=["Man Utd"])
        row = draft.filter(pl.col("team_code") == "3").row(0, named=True)
        assert row["footballdata_couk_name"] == "Man Utd"

    def test_an_ambiguous_name_is_left_unmatched(self) -> None:
        """Two FPL teams sharing a token with the same source name is a
        draft failure mode reserved for human review, never guessed at."""
        ambiguous_fpl_teams = pl.DataFrame(
            {"team_code": ["1", "2"], "canonical_name": ["Newcastle United", "United Town"]}
        )
        draft = draft_team_external_ids(ambiguous_fpl_teams, understat_names=["United"])
        assert draft["understat_name"].to_list() == [None, None]

    def test_multiple_source_names_for_one_club_are_joined_with_the_alias_separator(
        self,
    ) -> None:
        """openfootball spells the same club differently across seasons
        (``"Manchester City"`` vs ``"Manchester City FC"``) - both must
        resolve to the same team_code and both must survive into the cell,
        not just the first or last one seen."""
        draft = draft_team_external_ids(
            FPL_TEAMS_REAL,
            openfootball_names=["Manchester United", "Manchester United FC"],
        )
        row = draft.filter(pl.col("team_code") == "3").row(0, named=True)
        assert row["openfootball_name"] == "Manchester United; Manchester United FC"

    def test_the_same_alias_seen_twice_is_not_duplicated(self) -> None:
        draft = draft_team_external_ids(
            FPL_TEAMS_REAL, openfootball_names=["Arsenal FC", "Arsenal FC"]
        )
        row = draft.filter(pl.col("team_code") == "43").row(0, named=True)
        assert row["openfootball_name"] == "Arsenal FC"


class TestLoadWriteTeamExternalIds:
    def test_load_with_nothing_committed_returns_empty_typed_frame(self, tmp_path: Path) -> None:
        crosswalk = load_team_external_ids(data_root=tmp_path / "data")
        assert crosswalk.height == 0
        assert list(crosswalk.columns) == list(TEAM_EXTERNAL_ID_COLUMNS)

    def test_write_then_load_round_trips(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        draft = draft_team_external_ids(FPL_TEAMS_REAL, clubelo_names=["Arsenal"])
        write_team_external_ids(draft, data_root=data_root)

        loaded = load_team_external_ids(data_root=data_root)

        assert loaded.height == 3
        assert (
            loaded.filter(pl.col("team_code") == "43").row(0, named=True)["clubelo_name"]
            == "Arsenal"
        )


class TestRefreshTeamExternalIds:
    def test_never_overwrites_an_already_reviewed_row(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        reviewed = pl.DataFrame(
            {
                "team_code": ["43"],
                "clubelo_name": ["Arsenal FC (hand-corrected)"],
                "understat_name": [None],
                "footballdata_couk_name": [None],
                "openfootball_name": [None],
            }
        )
        write_team_external_ids(reviewed, data_root=data_root)

        refreshed = refresh_team_external_ids(
            FPL_TEAMS_REAL, clubelo_names=["Arsenal"], data_root=data_root
        )

        row = refreshed.filter(pl.col("team_code") == "43").row(0, named=True)
        assert row["clubelo_name"] == "Arsenal FC (hand-corrected)"

    def test_adds_a_row_for_a_team_code_not_yet_present(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        reviewed = pl.DataFrame(
            {
                "team_code": ["43"],
                "clubelo_name": ["Arsenal"],
                "understat_name": [None],
                "footballdata_couk_name": [None],
                "openfootball_name": [None],
            }
        )
        write_team_external_ids(reviewed, data_root=data_root)

        refreshed = refresh_team_external_ids(FPL_TEAMS_REAL, data_root=data_root)

        assert sorted(refreshed["team_code"].to_list()) == ["3", "43", "90"]

    def test_a_run_twice_is_a_no_op(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        first = refresh_team_external_ids(
            FPL_TEAMS_REAL, clubelo_names=["Arsenal"], data_root=data_root
        )
        write_team_external_ids(first, data_root=data_root)

        second = refresh_team_external_ids(
            FPL_TEAMS_REAL, clubelo_names=["Arsenal"], data_root=data_root
        )

        assert sorted(second["team_code"].to_list()) == sorted(first["team_code"].to_list())
        assert (
            second.filter(pl.col("team_code") == "43").row(0, named=True)["clubelo_name"]
            == "Arsenal"
        )


class TestUnmappedSourceNames:
    def test_a_name_with_no_crosswalk_row_is_flagged(self) -> None:
        crosswalk = pl.DataFrame(
            {
                "team_code": ["43"],
                "clubelo_name": ["Arsenal"],
                "understat_name": [None],
                "footballdata_couk_name": [None],
                "openfootball_name": [None],
            }
        )
        unmapped = unmapped_source_names(["Arsenal", "Mystery FC"], crosswalk, "clubelo_name")
        assert unmapped == ["Mystery FC"]

    def test_an_unknown_source_column_raises(self, tmp_path: Path) -> None:
        crosswalk = load_team_external_ids(data_root=tmp_path / "data")
        with pytest.raises(ValueError, match="unknown source column"):
            unmapped_source_names([], crosswalk, "team_code")

    def test_every_alias_in_a_multi_alias_cell_is_recognised(self) -> None:
        """A cell holding ``"Manchester City; Manchester City FC"`` must
        clear both spellings, not just the literal cell string."""
        crosswalk = pl.DataFrame(
            {
                "team_code": ["43"],
                "clubelo_name": [None],
                "understat_name": [None],
                "footballdata_couk_name": [None],
                "openfootball_name": ["Manchester City; Manchester City FC"],
            }
        )
        unmapped = unmapped_source_names(
            ["Manchester City", "Manchester City FC", "Mystery FC"],
            crosswalk,
            "openfootball_name",
        )
        assert unmapped == ["Mystery FC"]


class TestCollectSourceNames:
    def test_no_raw_captures_returns_empty_lists(self, tmp_path: Path) -> None:
        names = collect_source_names([SEASON], data_root=tmp_path / "data")
        assert names == {
            "clubelo_name": [],
            "footballdata_couk_name": [],
            "openfootball_name": [],
            "understat_name": [],
        }

    def test_clubelo_names_are_filtered_to_english_top_flight(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        body = (
            b"Rank,Club,Country,Level,Elo,From,To\n"
            b"1,Arsenal,ENG,1,2063.0,2026-05-31,2026-08-21\n"
            b"2,Real Madrid,ESP,1,1998.0,2026-05-31,2026-08-21\n"
            b"3,Leeds,ENG,2,1700.0,2026-05-31,2026-08-21\n"
        )
        _write_raw_csv(data_root, "clubelo", "ratings", SEASON, body)

        names = collect_source_names([SEASON], data_root=data_root)

        assert names["clubelo_name"] == ["Arsenal"]

    def test_footballdata_names_include_both_home_and_away(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        body = b"Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n16/08/2025,Arsenal,Man Utd,2,1,H\n"
        _write_raw_csv(data_root, "footballdata", "matches_and_odds", SEASON, body)

        names = collect_source_names([SEASON], data_root=data_root)

        assert names["footballdata_couk_name"] == ["Arsenal", "Man Utd"]

    def test_openfootball_names_are_filtered_to_english_clubs_only(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        body = (
            b"# Champions League 2025/26\n"
            b"\xe2\x96\xaa Matchday 1\n\n"
            b"Tue Sep 16 2025\n"
            b"    18:45  Athletic Club (ESP)     v Arsenal FC (ENG)         0-2\n"
        )
        _write_raw_txt(data_root, "openfootball", "champions_league", SEASON, body)

        names = collect_source_names([SEASON], data_root=data_root)

        assert names["openfootball_name"] == ["Arsenal FC"]
