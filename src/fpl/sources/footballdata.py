"""Connector for football-data.co.uk's per-season match-and-odds CSVs.

Simple GET, static CSV, no auth (spec §13) — mirrors
:class:`fpl.sources.fpl_api.FplApiConnector`'s "simple GET" shape rather
than :class:`fpl.sources.vaastav.VaastavConnector`'s tarball shape, since
one file covers exactly one season with no extraction step needed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fpl.config import Config, Season
from fpl.sources.errors import SchemaError
from fpl.sources.fetcher import HttpFetcher
from fpl.storage.raw_io import RawArtifact

__all__ = ["BASE_URL", "PREMIER_LEAGUE_DIVISION", "FootballDataConnector", "url_for_season"]

BASE_URL = "https://www.football-data.co.uk"

PREMIER_LEAGUE_DIVISION = "E0"
"""football-data.co.uk's own code for the Premier League — confirmed live
against ``mmz4281/2526/E0.csv`` during phase 7 probing (plan Finding C).
No other division is in scope for this project."""

_EXPECTED_HEADER_PREFIX = "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR"
"""Just enough of the confirmed live header to catch a genuinely different
file (an HTML error page, an empty response) without pinning down the
dozens of bookmaker-odds columns that vary release to release — the same
"shallow sanity check, not a schema" philosophy as every other connector's
header check."""


def url_for_season(season: Season, *, base_url: str = BASE_URL) -> str:
    """``mmz4281/{YY}{YY+1}/E0.csv`` — e.g. 2025/26 -> ``mmz4281/2526/E0.csv``."""
    yy_start = season.start_year % 100
    yy_end = season.end_year % 100
    path = f"mmz4281/{yy_start:02d}{yy_end:02d}/{PREMIER_LEAGUE_DIVISION}.csv"
    return f"{base_url.rstrip('/')}/{path}"


class FootballDataConnector:
    """Fetches one season's match-and-odds CSV at a time.

    ``VERSION`` follows the same convention as every other connector: bumped
    whenever the fetching logic changes in a way that could affect what
    lands on disk.
    """

    VERSION = "1"
    SOURCE = "footballdata"

    def __init__(
        self,
        *,
        fetcher: HttpFetcher | None = None,
        config: Config | None = None,
        base_url: str = BASE_URL,
        source_profile: str = "footballdata",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        loaded = config or Config.load()
        self._owns_fetcher = fetcher is None
        self.fetcher = fetcher or HttpFetcher(loaded.source(source_profile), loaded.user_agent)

    def __enter__(self) -> FootballDataConnector:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_fetcher:
            self.fetcher.close()

    def fetch_season(self, season: Season) -> bytes:
        url = url_for_season(season, base_url=self.base_url)
        response = self.fetcher.get(url)
        first_line = response.body.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
        if not first_line.startswith(_EXPECTED_HEADER_PREFIX):
            raise SchemaError(
                f"{url} did not return the expected football-data.co.uk header: {first_line!r}"
            )
        return response.body

    def artifact_for_season(self, body: bytes, season: Season) -> RawArtifact:
        """Wrap one season's CSV as a raw artifact, ready to write."""
        return RawArtifact(
            source=self.SOURCE,
            endpoint="matches_and_odds",
            season=season,
            url=url_for_season(season, base_url=self.base_url),
            http_status=200,
            body=body,
            fetched_at=datetime.now(UTC),
            connector_version=self.VERSION,
            content_type="csv",
        )
