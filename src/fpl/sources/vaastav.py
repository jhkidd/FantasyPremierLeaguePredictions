"""Connector for the vaastav/Fantasy-Premier-League community archive.

One tarball, not hundreds of file requests (spec plan §4.5 — the GitHub API
budget). The tarball itself is never persisted: only the per-season files we
actually need are extracted and written as their own content-addressed raw
artifacts. Re-running after an upstream correction rewrites only the seasons
whose bytes actually changed.
"""

from __future__ import annotations

import io
import tarfile
from datetime import UTC, datetime

from fpl.config import Config, Season
from fpl.sources.errors import SchemaError
from fpl.sources.fetcher import HttpFetcher
from fpl.storage.raw_io import RawArtifact, WriteResult, write_raw

__all__ = ["ARCHIVE_URL", "SEASON_FILES", "VaastavConnector"]

ARCHIVE_URL = "https://github.com/vaastav/Fantasy-Premier-League/archive/refs/heads/master.tar.gz"

SEASON_FILES: dict[str, str] = {
    "gws/merged_gw.csv": "merged_gw",
    "players_raw.csv": "players_raw",
    "teams.csv": "teams",
    "fixtures.csv": "fixtures",
}
"""Path within a season's directory -> our endpoint name.

``teams.csv`` and ``fixtures.csv`` are absent for the earliest seasons
(Finding 4) — that absence is expected and must not raise."""


class VaastavConnector:
    """Fetches one tarball and extracts the files one season needs.

    ``VERSION`` follows the same convention as :class:`FplApiConnector`:
    bumped whenever the fetching or extraction logic changes in a way that
    could affect what lands on disk.
    """

    VERSION = "1"
    SOURCE = "vaastav"

    def __init__(
        self,
        *,
        fetcher: HttpFetcher | None = None,
        config: Config | None = None,
        base_url: str = ARCHIVE_URL,
        source_profile: str = "vaastav",
    ) -> None:
        self.base_url = base_url
        loaded = config or Config.load()
        self._owns_fetcher = fetcher is None
        self.fetcher = fetcher or HttpFetcher(loaded.source(source_profile), loaded.user_agent)

    def __enter__(self) -> VaastavConnector:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_fetcher:
            self.fetcher.close()

    def fetch_tarball(self) -> bytes:
        """Download the whole archive. ~200MB; never written to disk as-is."""
        return self.fetcher.get(self.base_url).body

    def extract_season(self, tarball: bytes, season: Season) -> dict[str, bytes]:
        """Pull one season's files for :data:`SEASON_FILES` out of the tarball.

        Returns ``{relative_path: bytes}`` for whichever of those files exist.
        A season directory that does not exist at all in the archive is a
        real failure — every requested season must be classified by a person,
        never silently skipped — but a season missing one *file* is expected.
        """
        season_str = str(season)
        found: dict[str, bytes] = {}
        season_dir_seen = False

        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            for member in tar.getmembers():
                parts = member.name.split("/")
                # <repo>-master/data/<season>/<relative path...>
                if len(parts) < 4 or parts[1] != "data" or parts[2] != season_str:
                    continue
                season_dir_seen = True
                relative = "/".join(parts[3:])
                if relative not in SEASON_FILES or not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is not None:
                    found[relative] = extracted.read()

        if not season_dir_seen:
            raise SchemaError(
                f"vaastav archive has no data/{season_str}/ directory; "
                "a new or unrecognised season must be classified by a person"
            )
        return found

    def artifacts_for_season(self, tarball: bytes, season: Season) -> list[RawArtifact]:
        """Wrap each extracted file as a raw artifact, ready to write."""
        files = self.extract_season(tarball, season)
        fetched_at = datetime.now(UTC)
        artifacts = []
        for relative, body in files.items():
            endpoint = SEASON_FILES[relative]
            artifacts.append(
                RawArtifact(
                    source=self.SOURCE,
                    endpoint=endpoint,
                    season=season,
                    url=f"{self.base_url}#data/{season}/{relative}",
                    http_status=200,
                    body=body,
                    fetched_at=fetched_at,
                    connector_version=self.VERSION,
                    content_type="csv",
                )
            )
        return artifacts

    def fetch_and_store_season(
        self,
        season: Season,
        *,
        tarball: bytes | None = None,
        force: bool = False,
        data_root=None,
    ) -> list[WriteResult]:
        """Fetch (or reuse an already-fetched) tarball and store one season's files.

        ``tarball`` lets a backfill over many seasons fetch the ~200MB archive
        exactly once and reuse it, rather than once per season.
        """
        body = tarball if tarball is not None else self.fetch_tarball()
        artifacts = self.artifacts_for_season(body, season)
        return [write_raw(artifact, force=force, data_root=data_root) for artifact in artifacts]
