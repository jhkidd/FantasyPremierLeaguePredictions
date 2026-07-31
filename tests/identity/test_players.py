from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.identity.players import (
    PlayerCodeConflict,
    build_players_crosswalk,
    unmapped_players_with_minutes,
    validate_name_variants,
    write_players_crosswalk,
)
from fpl.staging.pipeline import stage_vaastav_source
from fpl.storage.raw_io import RawArtifact, write_raw

SEASON_1617 = Season(2016)
SEASON_1920 = Season(2019)
SEASON_2526 = Season(2025)

# Trimmed real excerpts of players_raw.csv across two seasons, carrying
# Finding 3's own example: code 123 keeps a stable `code` while its `id`
# changes and its recorded spelling gains an accent.
_PLAYERS_RAW_HEADER = "id,code,first_name,second_name,team,team_code,element_type\n"


def _write_players_raw(data_root: Path, season: Season, *rows: str) -> None:
    body = (_PLAYERS_RAW_HEADER + "".join(rows)).encode("utf-8")
    artifact = RawArtifact(
        source="vaastav",
        endpoint="players_raw",
        season=season,
        url="https://github.com/vaastav/Fantasy-Premier-League/.../players_raw.csv",
        http_status=200,
        body=body,
        fetched_at=datetime(2026, 7, 31, tzinfo=UTC),
        connector_version="1",
        content_type="csv",
    )
    write_raw(artifact, data_root=data_root)


class TestBuildPlayersCrosswalk:
    def test_no_ingested_seasons_returns_empty_frame(self, tmp_path: Path) -> None:
        crosswalk = build_players_crosswalk([SEASON_1617], data_root=tmp_path / "data")
        assert crosswalk.height == 0
        assert crosswalk.columns == [
            "player_code",
            "first_seen_season",
            "last_seen_season",
            "canonical_name",
            "name_variants",
            "seasons_seen",
        ]

    def test_same_code_across_seasons_collapses_to_one_row(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players_raw(data_root, SEASON_1617, "101,123,Muhamed,Besic,1,3,3\n")
        _write_players_raw(data_root, SEASON_1920, "55,123,Muhamed,Bešić,1,3,3\n")

        crosswalk = build_players_crosswalk([SEASON_1617, SEASON_1920], data_root=data_root)

        assert crosswalk.height == 1
        row = crosswalk.row(0, named=True)
        assert row["player_code"] == "123"
        assert row["first_seen_season"] == "2016-17"
        assert row["last_seen_season"] == "2019-20"
        assert row["seasons_seen"] == 2

    def test_canonical_name_is_the_most_recent_spelling(self, tmp_path: Path) -> None:
        """Finding 3: later spellings restore accents earlier ones stripped."""
        data_root = tmp_path / "data"
        _write_players_raw(data_root, SEASON_1617, "101,123,Muhamed,Besic,1,3,3\n")
        _write_players_raw(data_root, SEASON_1920, "55,123,Muhamed,Bešić,1,3,3\n")

        crosswalk = build_players_crosswalk([SEASON_1617, SEASON_1920], data_root=data_root)

        assert crosswalk.row(0, named=True)["canonical_name"] == "Muhamed Bešić"

    def test_distinct_codes_stay_distinct(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players_raw(
            data_root,
            SEASON_1617,
            "101,123,Muhamed,Besic,1,3,3\n",
            "102,456,Wayne,Hennessey,2,4,1\n",
        )

        crosswalk = build_players_crosswalk([SEASON_1617], data_root=data_root)

        assert crosswalk.height == 2
        assert set(crosswalk["player_code"].to_list()) == {"123", "456"}


class TestValidateNameVariants:
    def test_known_finding_3_variants_are_accepted(self) -> None:
        """Every variant Finding 3 actually found shares a name token and
        must not be flagged as a conflict."""
        crosswalk = pl.DataFrame(
            {
                "player_code": ["123", "789"],
                "name_variants": [
                    ["Muhamed Besic", "Muhamed Bešić"],
                    ["Matthew James", "Matty James"],
                ],
            }
        )

        assert validate_name_variants(crosswalk) == []

    def test_a_genuinely_reused_code_is_flagged(self) -> None:
        """Two spellings sharing no token at all looks like a reused code,
        not a spelling correction, and must be surfaced for review."""
        crosswalk = pl.DataFrame(
            {
                "player_code": ["999"],
                "name_variants": [["Wayne Hennessey", "Kepa Arrizabalaga"]],
            }
        )

        conflicts = validate_name_variants(crosswalk)

        assert conflicts == [
            PlayerCodeConflict("999", ("Wayne Hennessey", "Kepa Arrizabalaga"))
        ]

    def test_a_single_spelling_is_never_a_conflict(self) -> None:
        crosswalk = pl.DataFrame(
            {"player_code": ["123"], "name_variants": [["Muhamed Bešić"]]}
        )

        assert validate_name_variants(crosswalk) == []


class TestUnmappedPlayersWithMinutes:
    _MERGED_GW_HEADER = (
        "name,position,team,xP,assists,bonus,bps,clean_sheets,creativity,element,"
        "expected_assists,expected_goal_involvements,expected_goals,expected_goals_conceded,"
        "fixture,goals_conceded,goals_scored,ict_index,influence,kickoff_time,minutes,modified,"
        "opponent_team,own_goals,penalties_missed,penalties_saved,red_cards,round,saves,selected,"
        "starts,team_a_score,team_h_score,threat,total_points,transfers_balance,transfers_in,"
        "transfers_out,value,was_home,yellow_cards,clearances_blocks_interceptions,"
        "defensive_contribution,recoveries,tackles,GW\n"
    )
    _PLAYED_ROW = (
        "Reinildo Mandava,DEF,Sunderland,0.5,0,0,27,1,2.5,541,0.00,0.00,0.00,0.56,5,0,0,1.6,"
        "13.6,2025-08-16T14:00:00Z,90,False,19,0,0,0,0,1,0,677026,1,0,3,0.0,6,0,0,0,40,True,0,6,"
        "8,3,2,1\n"
    )

    def _write_merged_gw(self, data_root: Path, season: Season) -> None:
        body = (self._MERGED_GW_HEADER + self._PLAYED_ROW).encode("utf-8")
        artifact = RawArtifact(
            source="vaastav",
            endpoint="merged_gw",
            season=season,
            url="https://github.com/vaastav/Fantasy-Premier-League/.../gws/merged_gw.csv",
            http_status=200,
            body=body,
            fetched_at=datetime(2026, 7, 31, tzinfo=UTC),
            connector_version="1",
            content_type="csv",
        )
        write_raw(artifact, data_root=data_root)

    def _write_players_raw(self, data_root: Path, season: Season) -> None:
        body = (
            b"id,code,first_name,second_name,team,team_code,element_type\n"
            b"541,3,Reinildo,Mandava,19,20,2\n"
        )
        artifact = RawArtifact(
            source="vaastav",
            endpoint="players_raw",
            season=season,
            url="https://github.com/vaastav/Fantasy-Premier-League/.../players_raw.csv",
            http_status=200,
            body=body,
            fetched_at=datetime(2026, 7, 31, tzinfo=UTC),
            connector_version="1",
            content_type="csv",
        )
        write_raw(artifact, data_root=data_root)

    def test_no_facts_yields_no_unmapped_players(self, tmp_path: Path) -> None:
        crosswalk = pl.DataFrame({"player_code": pl.Series([], dtype=pl.Utf8)})
        assert unmapped_players_with_minutes(SEASON_2526, crosswalk, data_root=tmp_path) == []

    def test_a_player_with_minutes_and_no_crosswalk_entry_is_unmapped(
        self, tmp_path: Path
    ) -> None:
        data_root = tmp_path / "data"
        self._write_merged_gw(data_root, SEASON_2526)
        stage_vaastav_source(SEASON_2526, data_root=data_root)
        crosswalk = pl.DataFrame({"player_code": pl.Series([], dtype=pl.Utf8)})

        unmapped = unmapped_players_with_minutes(SEASON_2526, crosswalk, data_root=data_root)

        assert unmapped == [541]

    def test_a_mapped_player_with_minutes_is_not_unmapped(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        self._write_merged_gw(data_root, SEASON_2526)
        self._write_players_raw(data_root, SEASON_2526)
        stage_vaastav_source(SEASON_2526, data_root=data_root)
        crosswalk = pl.DataFrame({"player_code": ["3"]})

        unmapped = unmapped_players_with_minutes(SEASON_2526, crosswalk, data_root=data_root)

        assert unmapped == []


class TestWritePlayersCrosswalk:
    def test_writes_a_csv_with_joined_name_variants(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        crosswalk = pl.DataFrame(
            {
                "player_code": ["123"],
                "first_seen_season": ["2016-17"],
                "last_seen_season": ["2019-20"],
                "canonical_name": ["Muhamed Bešić"],
                "name_variants": [["Muhamed Besic", "Muhamed Bešić"]],
                "seasons_seen": [2],
            }
        )

        out_path = write_players_crosswalk(crosswalk, data_root=data_root)

        assert out_path.is_file()
        contents = out_path.read_text(encoding="utf-8")
        assert "Muhamed Besic; Muhamed Bešić" in contents
