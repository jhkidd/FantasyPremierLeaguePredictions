from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from fpl.config import Season
from fpl.sources.errors import SchemaError
from fpl.sources.vaastav import VaastavConnector
from fpl.storage.raw_io import read_raw


def _make_tarball(files: dict[str, bytes], *, root: str = "Fantasy-Premier-League-master") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, body in files.items():
            info = tarfile.TarInfo(name=f"{root}/{path}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


@pytest.fixture
def connector() -> VaastavConnector:
    return VaastavConnector()


class TestExtractSeason:
    def test_extracts_present_files(self, connector: VaastavConnector) -> None:
        tarball = _make_tarball(
            {
                "data/2025-26/gws/merged_gw.csv": b"a,b\n1,2\n",
                "data/2025-26/players_raw.csv": b"id,code\n1,100\n",
                "data/2025-26/teams.csv": b"id,name\n1,Arsenal\n",
                "data/2025-26/fixtures.csv": b"id\n1\n",
            }
        )
        files = connector.extract_season(tarball, Season(2025))
        assert files["gws/merged_gw.csv"] == b"a,b\n1,2\n"
        assert files["players_raw.csv"] == b"id,code\n1,100\n"
        assert files["teams.csv"] == b"id,name\n1,Arsenal\n"
        assert files["fixtures.csv"] == b"id\n1\n"

    def test_missing_teams_and_fixtures_is_not_an_error(self, connector: VaastavConnector) -> None:
        """Finding 4: teams.csv and fixtures.csv are absent for the earliest seasons."""
        tarball = _make_tarball(
            {
                "data/2016-17/gws/merged_gw.csv": b"a,b\n1,2\n",
                "data/2016-17/players_raw.csv": b"id,code\n1,100\n",
            }
        )
        files = connector.extract_season(tarball, Season(2016))
        assert set(files) == {"gws/merged_gw.csv", "players_raw.csv"}

    def test_unrecognised_season_raises(self, connector: VaastavConnector) -> None:
        tarball = _make_tarball({"data/2025-26/players_raw.csv": b"id\n1\n"})
        with pytest.raises(SchemaError, match="2030-31"):
            connector.extract_season(tarball, Season(2030))

    def test_unrelated_files_are_ignored(self, connector: VaastavConnector) -> None:
        tarball = _make_tarball(
            {
                "data/2025-26/players_raw.csv": b"id\n1\n",
                "data/2025-26/id_dict.csv": b"id,code\n1,100\n",
                "README.md": b"hello",
            }
        )
        files = connector.extract_season(tarball, Season(2025))
        assert set(files) == {"players_raw.csv"}


class TestArtifactsForSeason:
    def test_builds_one_artifact_per_extracted_file(self, connector: VaastavConnector) -> None:
        tarball = _make_tarball(
            {
                "data/2025-26/players_raw.csv": b"id\n1\n",
                "data/2025-26/teams.csv": b"id\n1\n",
            }
        )
        artifacts = connector.artifacts_for_season(tarball, Season(2025))
        endpoints = {a.endpoint for a in artifacts}
        assert endpoints == {"players_raw", "teams"}
        assert all(a.source == "vaastav" for a in artifacts)
        assert all(a.season == Season(2025) for a in artifacts)


class TestFetchAndStoreSeason:
    def test_writes_each_extracted_file_as_raw(
        self, connector: VaastavConnector, tmp_path: Path
    ) -> None:
        tarball = _make_tarball(
            {
                "data/2025-26/players_raw.csv": b"id\n1\n",
                "data/2025-26/teams.csv": b"id\n1\n",
            }
        )
        results = connector.fetch_and_store_season(
            Season(2025), tarball=tarball, data_root=tmp_path
        )
        assert all(result.written for result in results)
        for result in results:
            body, meta = read_raw(result.path)
            assert meta["source"] == "vaastav"
            assert body

    def test_unchanged_bytes_on_second_run_write_nothing(
        self, connector: VaastavConnector, tmp_path: Path
    ) -> None:
        tarball = _make_tarball({"data/2025-26/players_raw.csv": b"id\n1\n"})
        first = connector.fetch_and_store_season(Season(2025), tarball=tarball, data_root=tmp_path)
        second = connector.fetch_and_store_season(Season(2025), tarball=tarball, data_root=tmp_path)
        assert all(r.written for r in first)
        assert all(not r.written for r in second)

    def test_reuses_a_supplied_tarball_without_refetching(
        self, connector: VaastavConnector, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tarball = _make_tarball({"data/2025-26/players_raw.csv": b"id\n1\n"})

        def _boom() -> bytes:
            raise AssertionError("fetch_tarball should not be called when tarball is supplied")

        monkeypatch.setattr(connector, "fetch_tarball", _boom)
        connector.fetch_and_store_season(Season(2025), tarball=tarball, data_root=tmp_path)
