from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fpl.config import Season
from fpl.identity.teams import build_teams_crosswalk, write_teams_crosswalk
from fpl.storage.raw_io import RawArtifact, write_raw

SEASON_1617 = Season(2016)  # no teams.csv; resolved via players_raw + hand-verified codes
SEASON_2526 = Season(2025)  # real teams.csv


def _write_teams_csv(data_root: Path, season: Season, *rows: str) -> None:
    body = ("id,code,name\n" + "".join(rows)).encode("utf-8")
    artifact = RawArtifact(
        source="vaastav",
        endpoint="teams",
        season=season,
        url="https://github.com/vaastav/Fantasy-Premier-League/.../teams.csv",
        http_status=200,
        body=body,
        fetched_at=datetime(2026, 7, 31, tzinfo=UTC),
        connector_version="1",
        content_type="csv",
    )
    write_raw(artifact, data_root=data_root)


def _write_players_raw(data_root: Path, season: Season, *rows: str) -> None:
    body = ("id,code,first_name,second_name,team,team_code,element_type\n" + "".join(rows)).encode(
        "utf-8"
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


class TestBuildTeamsCrosswalk:
    def test_no_ingested_seasons_returns_empty_frame(self, tmp_path: Path) -> None:
        crosswalk = build_teams_crosswalk([SEASON_2526], data_root=tmp_path / "data")
        assert crosswalk.height == 0
        assert crosswalk.columns == ["season", "team_id", "team_code", "canonical_name"]

    def test_reads_teams_csv_directly_when_present(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_teams_csv(data_root, SEASON_2526, "3,90,Bournemouth\n", "91,43,Arsenal\n")

        crosswalk = build_teams_crosswalk([SEASON_2526], data_root=data_root)

        assert crosswalk.height == 2
        by_id = {row["team_id"]: row for row in crosswalk.iter_rows(named=True)}
        assert by_id[3]["team_code"] == "90"
        assert by_id[3]["canonical_name"] == "Bournemouth"

    def test_falls_back_to_players_raw_when_no_teams_csv(self, tmp_path: Path) -> None:
        """2016/17-2018/19 have no teams.csv; team_id/team_code come from
        players_raw and the name from a hand-verified code (Stoke=110)."""
        data_root = tmp_path / "data"
        _write_players_raw(data_root, SEASON_1617, "1,50,Jack,Butland,7,110,1\n")

        crosswalk = build_teams_crosswalk([SEASON_1617], data_root=data_root)

        assert crosswalk.height == 1
        row = crosswalk.row(0, named=True)
        assert row["team_id"] == 7
        assert row["team_code"] == "110"
        assert row["canonical_name"] == "Stoke"

    def test_a_players_raw_only_code_with_no_known_name_raises(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players_raw(data_root, SEASON_1617, "1,50,Jack,Butland,7,99999,1\n")

        with pytest.raises(ValueError, match="99999"):
            build_teams_crosswalk([SEASON_1617], data_root=data_root)

    def test_inconsistent_code_to_name_across_seasons_raises(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_teams_csv(data_root, Season(2019), "1,43,Arsenal\n")
        _write_teams_csv(data_root, Season(2020), "1,43,Not Arsenal\n")

        with pytest.raises(ValueError, match="43"):
            build_teams_crosswalk([Season(2019), Season(2020)], data_root=data_root)


class TestWriteTeamsCrosswalk:
    def test_writes_a_csv(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_teams_csv(data_root, SEASON_2526, "3,90,Bournemouth\n")
        crosswalk = build_teams_crosswalk([SEASON_2526], data_root=data_root)

        out_path = write_teams_crosswalk(crosswalk, data_root=data_root)

        assert out_path.is_file()
        assert "Bournemouth" in out_path.read_text(encoding="utf-8")
