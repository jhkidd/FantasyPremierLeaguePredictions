from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from fpl.config import Season
from fpl.sources.errors import SchemaError
from fpl.sources.openfootball import OpenfootballConnector
from fpl.storage.raw_io import read_raw


def _make_tarball(files: dict[str, bytes], *, root: str = "champions-league-master") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, body in files.items():
            info = tarfile.TarInfo(name=f"{root}/{path}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


@pytest.fixture
def connector() -> OpenfootballConnector:
    return OpenfootballConnector()


class TestExtractSeason:
    def test_extracts_present_files(self, connector: OpenfootballConnector) -> None:
        tarball = _make_tarball(
            {
                "2025-26/cl.txt": b"cl body",
                "2025-26/clq.txt": b"clq body",
                "2025-26/elq.txt": b"elq body",
                "2025-26/confq.txt": b"confq body",
            }
        )
        files = connector.extract_season(tarball, Season(2025))
        assert files["cl.txt"] == b"cl body"
        assert files["clq.txt"] == b"clq body"
        assert files["elq.txt"] == b"elq body"
        assert files["confq.txt"] == b"confq body"

    def test_a_season_with_no_conference_league_qualifying_is_not_an_error(
        self, connector: OpenfootballConnector
    ) -> None:
        """No club a season tracks reaching, say, the Conference League
        qualifying rounds is a legitimate silent absence, not a defect -
        unlike vaastav.SEASON_FILES, nothing here is required."""
        tarball = _make_tarball({"2025-26/cl.txt": b"cl body"})
        files = connector.extract_season(tarball, Season(2025))
        assert set(files) == {"cl.txt"}

    def test_unrecognised_season_raises(self, connector: OpenfootballConnector) -> None:
        tarball = _make_tarball({"2025-26/cl.txt": b"cl body"})
        with pytest.raises(SchemaError, match="2030-31"):
            connector.extract_season(tarball, Season(2030))

    def test_unrelated_files_are_ignored(self, connector: OpenfootballConnector) -> None:
        tarball = _make_tarball(
            {
                "2025-26/cl.txt": b"cl body",
                "2025-26/el.txt": b"not yet confirmed, so not extracted",
                "README.md": b"hello",
            }
        )
        files = connector.extract_season(tarball, Season(2025))
        assert set(files) == {"cl.txt"}


class TestArtifactsForSeason:
    def test_builds_one_artifact_per_extracted_file(self, connector: OpenfootballConnector) -> None:
        tarball = _make_tarball(
            {
                "2025-26/cl.txt": b"cl body",
                "2025-26/clq.txt": b"clq body",
            }
        )
        artifacts = connector.artifacts_for_season(tarball, Season(2025))
        endpoints = {a.endpoint for a in artifacts}
        assert endpoints == {"champions_league", "champions_league_qualifying"}
        assert all(a.source == "openfootball" for a in artifacts)
        assert all(a.season == Season(2025) for a in artifacts)
        assert all(a.content_type == "txt" for a in artifacts)


class TestFetchAndStoreSeason:
    def test_writes_each_extracted_file_as_raw(
        self, connector: OpenfootballConnector, tmp_path: Path
    ) -> None:
        tarball = _make_tarball({"2025-26/cl.txt": b"cl body"})
        results = connector.fetch_and_store_season(
            Season(2025), tarball=tarball, data_root=tmp_path
        )
        assert all(result.written for result in results)
        for result in results:
            body, meta = read_raw(result.path)
            assert meta["source"] == "openfootball"
            assert body

    def test_reuses_a_supplied_tarball_without_refetching(
        self, connector: OpenfootballConnector, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tarball = _make_tarball({"2025-26/cl.txt": b"cl body"})

        def _boom() -> bytes:
            raise AssertionError("fetch_tarball should not be called when tarball is supplied")

        monkeypatch.setattr(connector, "fetch_tarball", _boom)
        connector.fetch_and_store_season(Season(2025), tarball=tarball, data_root=tmp_path)
