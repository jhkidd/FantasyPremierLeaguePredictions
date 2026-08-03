from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.identity.players_understat import (
    PLAYERS_UNDERSTAT_COLUMNS,
    draft_players_crosswalk,
    load_players_understat_crosswalk,
    refresh_players_crosswalk,
    unmapped_understat_players_with_minutes,
    write_players_crosswalk,
)
from fpl.storage.raw_io import RawArtifact, write_raw

SEASON = Season(2025)

PLAYERS_RAW_CSV = (
    b"code,first_name,second_name\n620,Bruno,Fernandes\n999,Marcus,Rashford\n111,Joshua,King\n"
)


def _understat_league_data(players: list[dict]) -> dict:
    return {"teams": {}, "players": players, "dates": []}


def _understat_player(player_id: str, name: str, *, minutes: str = "1800") -> dict:
    return {
        "id": player_id,
        "player_name": name,
        "team_title": "Manchester United",
        "position": "M",
        "games": "20",
        "time": minutes,
        "goals": "5",
        "xG": "4.5",
        "assists": "3",
        "xA": "2.5",
        "shots": "40",
        "key_passes": "30",
        "yellow_cards": "1",
        "red_cards": "0",
        "npg": "5",
        "npxG": "4.0",
        "xGChain": "6.0",
        "xGBuildup": "3.0",
    }


def _write_players_raw(data_root: Path, season: Season, body: bytes = PLAYERS_RAW_CSV) -> None:
    artifact = RawArtifact(
        source="vaastav",
        endpoint="players_raw",
        season=season,
        url="https://example.invalid/players_raw.csv",
        http_status=200,
        body=body,
        fetched_at=datetime(2026, 8, 15, tzinfo=UTC),
        connector_version="1",
        content_type="csv",
    )
    write_raw(artifact, data_root=data_root)


def _write_understat_league_data(data_root: Path, season: Season, players: list[dict]) -> None:
    artifact = RawArtifact(
        source="understat",
        endpoint="league_data",
        season=season,
        url="https://understat.com/getLeagueData/EPL/2025",
        http_status=200,
        body=json.dumps(_understat_league_data(players)).encode(),
        fetched_at=datetime(2026, 8, 15, tzinfo=UTC),
        connector_version="1",
        content_type="json",
    )
    write_raw(artifact, data_root=data_root)


class TestDraftPlayersCrosswalk:
    def test_unambiguous_name_match_is_drafted(self, tmp_path: Path) -> None:
        _write_players_raw(tmp_path, SEASON)
        _write_understat_league_data(
            tmp_path, SEASON, [_understat_player("620", "Bruno Fernandes")]
        )
        draft = draft_players_crosswalk([SEASON], data_root=tmp_path)
        row = draft.filter(pl.col("player_code") == "620").row(0, named=True)
        assert row["understat_player_id"] == 620
        assert row["understat_name"] == "Bruno Fernandes"

    def test_every_fpl_code_gets_a_row_even_with_no_match(self, tmp_path: Path) -> None:
        _write_players_raw(tmp_path, SEASON)
        _write_understat_league_data(tmp_path, SEASON, [])
        draft = draft_players_crosswalk([SEASON], data_root=tmp_path)
        assert set(draft["player_code"].to_list()) == {"620", "999", "111"}
        assert draft["understat_player_id"].is_null().all()

    def test_ambiguous_name_collision_is_left_unresolved(self, tmp_path: Path) -> None:
        """Two different real 'Joshua King's - the same collision found
        live during probing - must not be guessed at."""
        _write_players_raw(tmp_path, SEASON)
        _write_understat_league_data(
            tmp_path,
            SEASON,
            [_understat_player("111", "Joshua King"), _understat_player("222", "Joshua King")],
        )
        draft = draft_players_crosswalk([SEASON], data_root=tmp_path)
        row = draft.filter(pl.col("player_code") == "111").row(0, named=True)
        assert row["understat_player_id"] is None

    def test_shared_first_name_alone_is_not_a_false_collision(self, tmp_path: Path) -> None:
        """A live-probe regression: 'James Ward-Prowse' sharing the token
        'James' with several unrelated Understat players ('James Milner',
        'James Tomkins', ...) must not block the match - only a shared
        surname counts as a genuine collision."""
        _write_players_raw(
            tmp_path,
            SEASON,
            body=(b"code,first_name,second_name\n620,James,Ward-Prowse\n"),
        )
        _write_understat_league_data(
            tmp_path,
            SEASON,
            [
                _understat_player("843", "James Ward-Prowse"),
                _understat_player("999", "James Milner"),
                _understat_player("998", "James Tomkins"),
            ],
        )
        draft = draft_players_crosswalk([SEASON], data_root=tmp_path)
        row = draft.filter(pl.col("player_code") == "620").row(0, named=True)
        assert row["understat_player_id"] == 843
        assert row["understat_name"] == "James Ward-Prowse"

    def test_no_ingested_season_yields_an_empty_frame(self, tmp_path: Path) -> None:
        draft = draft_players_crosswalk([SEASON], data_root=tmp_path)
        assert draft.height == 0
        assert list(draft.columns) == list(PLAYERS_UNDERSTAT_COLUMNS)


class TestRefreshPlayersCrosswalk:
    def test_never_overwrites_an_already_reviewed_row(self, tmp_path: Path) -> None:
        _write_players_raw(tmp_path, SEASON)
        _write_understat_league_data(
            tmp_path, SEASON, [_understat_player("620", "Bruno Fernandes")]
        )
        reviewed = pl.DataFrame(
            {
                "player_code": ["620"],
                "fpl_name": ["Bruno Fernandes"],
                "understat_player_id": [12345],  # a human correction
                "understat_name": ["Bruno Miguel Borges Fernandes"],
            },
            schema={
                "player_code": pl.Utf8,
                "fpl_name": pl.Utf8,
                "understat_player_id": pl.Int64,
                "understat_name": pl.Utf8,
            },
        )
        write_players_crosswalk(reviewed, data_root=tmp_path)

        refreshed = refresh_players_crosswalk([SEASON], data_root=tmp_path)
        row = refreshed.filter(pl.col("player_code") == "620").row(0, named=True)
        assert row["understat_player_id"] == 12345

    def test_adds_a_row_for_a_code_not_yet_present(self, tmp_path: Path) -> None:
        _write_players_raw(tmp_path, SEASON)
        _write_understat_league_data(tmp_path, SEASON, [])
        write_players_crosswalk(
            pl.DataFrame(
                schema={
                    "player_code": pl.Utf8,
                    "fpl_name": pl.Utf8,
                    "understat_player_id": pl.Int64,
                    "understat_name": pl.Utf8,
                }
            ),
            data_root=tmp_path,
        )
        refreshed = refresh_players_crosswalk([SEASON], data_root=tmp_path)
        assert set(refreshed["player_code"].to_list()) == {"620", "999", "111"}


class TestLoadPlayersUnderstatCrosswalk:
    def test_no_committed_file_returns_an_empty_typed_frame(self, tmp_path: Path) -> None:
        loaded = load_players_understat_crosswalk(data_root=tmp_path)
        assert loaded.height == 0
        assert list(loaded.columns) == list(PLAYERS_UNDERSTAT_COLUMNS)


class TestUnmappedUnderstatPlayersWithMinutes:
    def test_a_played_player_absent_from_the_crosswalk_is_flagged(self, tmp_path: Path) -> None:
        _write_understat_league_data(
            tmp_path, SEASON, [_understat_player("620", "Bruno Fernandes", minutes="1800")]
        )
        empty_crosswalk = pl.DataFrame(
            schema={
                "player_code": pl.Utf8,
                "fpl_name": pl.Utf8,
                "understat_player_id": pl.Int64,
                "understat_name": pl.Utf8,
            }
        )
        unmapped = unmapped_understat_players_with_minutes(
            SEASON, empty_crosswalk, data_root=tmp_path
        )
        assert unmapped == [620]

    def test_a_mapped_player_is_not_flagged(self, tmp_path: Path) -> None:
        _write_understat_league_data(
            tmp_path, SEASON, [_understat_player("620", "Bruno Fernandes", minutes="1800")]
        )
        crosswalk = pl.DataFrame(
            {
                "player_code": ["620"],
                "fpl_name": ["Bruno Fernandes"],
                "understat_player_id": [620],
                "understat_name": ["Bruno Fernandes"],
            },
            schema={
                "player_code": pl.Utf8,
                "fpl_name": pl.Utf8,
                "understat_player_id": pl.Int64,
                "understat_name": pl.Utf8,
            },
        )
        assert unmapped_understat_players_with_minutes(SEASON, crosswalk, data_root=tmp_path) == []

    def test_a_played_player_with_zero_minutes_is_never_flagged(self, tmp_path: Path) -> None:
        _write_understat_league_data(
            tmp_path, SEASON, [_understat_player("620", "Bruno Fernandes", minutes="0")]
        )
        empty_crosswalk = pl.DataFrame(
            schema={
                "player_code": pl.Utf8,
                "fpl_name": pl.Utf8,
                "understat_player_id": pl.Int64,
                "understat_name": pl.Utf8,
            }
        )
        assert (
            unmapped_understat_players_with_minutes(SEASON, empty_crosswalk, data_root=tmp_path)
            == []
        )

    def test_no_understat_capture_yields_nothing(self, tmp_path: Path) -> None:
        empty_crosswalk = pl.DataFrame(
            schema={
                "player_code": pl.Utf8,
                "fpl_name": pl.Utf8,
                "understat_player_id": pl.Int64,
                "understat_name": pl.Utf8,
            }
        )
        assert (
            unmapped_understat_players_with_minutes(SEASON, empty_crosswalk, data_root=tmp_path)
            == []
        )
