"""Connector for `openfootball/champions-league` (European fixture schedules).

Domestic cups (FA Cup, EFL Cup) are explicitly **not** this connector's job —
`openfootball/england` was checked live during phase 7 probing and found to
carry only league divisions, with domestic cups present only as one-off
RSSSF archival snapshots, never maintained per-season data (plan Finding B).
Those come from football-data.org instead (`sources/footballdataorg.py`).

Same tarball-not-hundreds-of-requests shape as `sources/vaastav.py`: one
archive fetch, never persisted whole, only the season's own files extracted
as their own content-addressed raw artifacts.
"""

from __future__ import annotations

import io
import tarfile
from datetime import UTC, datetime

from fpl.config import Config, Season
from fpl.sources.errors import SchemaError
from fpl.sources.fetcher import HttpFetcher
from fpl.storage.raw_io import RawArtifact, WriteResult, write_raw

__all__ = ["ARCHIVE_URL", "SEASON_FILES", "OpenfootballConnector"]

ARCHIVE_URL = "https://github.com/openfootball/champions-league/archive/refs/heads/master.tar.gz"

SEASON_FILES: dict[str, str] = {
    "cl.txt": "champions_league",
    "clq.txt": "champions_league_qualifying",
    "elq.txt": "europa_league_qualifying",
    "confq.txt": "conference_league_qualifying",
}
"""File within a season's directory -> our endpoint name.

``el.txt`` (Europa League group/knockout stage) is referenced in the
repository's own README but was not independently confirmed present in a
directory listing during probing, so it is deliberately left out here
rather than guessed at — a future confirmed sighting can add it.

Unlike :data:`fpl.sources.vaastav.SEASON_FILES`, **none of these are
required at the per-season level**: a season where no tracked club reached,
say, the Conference League qualifying rounds is a legitimate, silent
absence, not a defect. What *is* required, mirroring
:meth:`fpl.sources.vaastav.VaastavConnector.extract_season`, is that the
season's own directory exists in the archive at all."""


class OpenfootballConnector:
    """Fetches one tarball and extracts the season's own files.

    ``VERSION`` follows the same convention as every other connector: bumped
    whenever the fetching or extraction logic changes in a way that could
    affect what lands on disk.
    """

    VERSION = "1"
    SOURCE = "openfootball"

    def __init__(
        self,
        *,
        fetcher: HttpFetcher | None = None,
        config: Config | None = None,
        base_url: str = ARCHIVE_URL,
        source_profile: str = "openfootball",
    ) -> None:
        self.base_url = base_url
        loaded = config or Config.load()
        self._owns_fetcher = fetcher is None
        self.fetcher = fetcher or HttpFetcher(loaded.source(source_profile), loaded.user_agent)

    def __enter__(self) -> OpenfootballConnector:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_fetcher:
            self.fetcher.close()

    def fetch_tarball(self) -> bytes:
        """Download the whole archive. Never written to disk as-is."""
        return self.fetcher.get(self.base_url).body

    def extract_season(self, tarball: bytes, season: Season) -> dict[str, bytes]:
        """Pull whichever of :data:`SEASON_FILES` exist for one season.

        Returns ``{relative_path: bytes}``. A season directory that does not
        exist at all in the archive is a real failure — every requested
        season must be classified by a person, never silently skipped — but
        a season missing one *file* is expected and not an error.
        """
        season_str = str(season)
        found: dict[str, bytes] = {}
        season_dir_seen = False

        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            for member in tar.getmembers():
                parts = member.name.split("/")
                # <repo>-master/<season>/<relative path...>
                if len(parts) < 3 or parts[1] != season_str:
                    continue
                season_dir_seen = True
                relative = "/".join(parts[2:])
                if relative not in SEASON_FILES or not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is not None:
                    found[relative] = extracted.read()

        if not season_dir_seen:
            raise SchemaError(
                f"openfootball/champions-league archive has no {season_str}/ directory; "
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
                    url=f"{self.base_url}#{season}/{relative}",
                    http_status=200,
                    body=body,
                    fetched_at=fetched_at,
                    connector_version=self.VERSION,
                    content_type="txt",
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

        ``tarball`` lets a backfill over many seasons fetch the archive
        exactly once and reuse it, rather than once per season - mirrors
        :meth:`fpl.sources.vaastav.VaastavConnector.fetch_and_store_season`.
        """
        body = tarball if tarball is not None else self.fetch_tarball()
        artifacts = self.artifacts_for_season(body, season)
        return [write_raw(artifact, force=force, data_root=data_root) for artifact in artifacts]
