"""Connector for Understat's undocumented AJAX endpoints (plan §7.10-7.11).

**Supersedes the plan's original design.** §7.10-7.11 as written assumed
Understat's team/league pages embed a JSON blob in a ``<script>`` tag - that
was true once, but a live probe during phase 7 (2026-08-04) found the
current site no longer does this at all; the redesigned front end fetches
its data client-side instead. The real, currently-live mechanism (confirmed
via ``curl`` against ``understat.com`` directly, cross-checked against the
third-party ``understatAPI`` project's source as a map of what to probe):

- Every request must carry ``X-Requested-With: XMLHttpRequest`` - omitting
  it returns a 404, not an error page, so a missing header looks exactly
  like "this endpoint does not exist" until you know to check.
- ``getLeagueData/{league}/{season}`` returns one season's **aggregate**
  stats: ``teams``, a season-total row per ``players``, and ``dates`` - one
  entry per fixture with ids, teams, final score and xG, but no per-player
  detail.
- ``getMatchData/{match_id}`` returns one fixture's **per-player** detail:
  ``rosters.h``/``rosters.a`` carry every player who featured, with
  per-match minutes/goals/xG/xA/shots.

Only EPL is in scope (confirmed with the user 2026-08-04) - the original
design spec's six-league scope is deliberately not built yet: the per-match
request volume this connector needs makes "the others cost little" no
longer true.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fpl.config import Config, Season
from fpl.sources.errors import SchemaError
from fpl.sources.fetcher import HttpFetcher
from fpl.storage.raw_io import RawArtifact

__all__ = ["BASE_URL", "LEAGUE", "UnderstatConnector"]

BASE_URL = "https://understat.com"

LEAGUE = "EPL"
"""Understat's own code for the Premier League. The only league this
connector fetches (plan §7.10 scope decision, 2026-08-04)."""

_AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
"""Required on every Understat AJAX call - confirmed live: the same URL
without this header returns a 404, not a 4xx carrying any useful body."""

_LEAGUE_DATA_KEYS = ("teams", "players", "dates")
_MATCH_DATA_KEYS = ("rosters",)
_ROSTER_SIDES = ("h", "a")


class UnderstatConnector:
    """Fetches Understat's per-season aggregate and per-match detail data.

    ``VERSION`` follows the same convention as every other connector: bumped
    whenever the fetching logic changes in a way that could affect what
    lands on disk.
    """

    VERSION = "1"
    SOURCE = "understat"

    def __init__(
        self,
        *,
        fetcher: HttpFetcher | None = None,
        config: Config | None = None,
        base_url: str = BASE_URL,
        source_profile: str = "understat",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        loaded = config or Config.load()
        self._owns_fetcher = fetcher is None
        self.fetcher = fetcher or HttpFetcher(loaded.source(source_profile), loaded.user_agent)

    def __enter__(self) -> UnderstatConnector:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_fetcher:
            self.fetcher.close()

    def league_data_url(self, season: Season) -> str:
        """``getLeagueData/EPL/{start_year}`` - Understat identifies a season
        by the calendar year it starts in, same convention as :class:`Season`."""
        return f"{self.base_url}/getLeagueData/{LEAGUE}/{season.start_year}"

    def match_data_url(self, match_id: int) -> str:
        return f"{self.base_url}/getMatchData/{match_id}"

    def fetch_league_data(self, season: Season) -> bytes:
        """One season's aggregate players/teams/fixture-list payload."""
        url = self.league_data_url(season)
        response = self.fetcher.get(url, headers=_AJAX_HEADERS)
        payload = _parse_json(response.body, url)
        _check_keys(payload, _LEAGUE_DATA_KEYS, url, what="league data")
        return response.body

    def fetch_match_data(self, match_id: int) -> bytes:
        """One fixture's per-player roster detail."""
        url = self.match_data_url(match_id)
        response = self.fetcher.get(url, headers=_AJAX_HEADERS)
        payload = _parse_json(response.body, url)
        _check_keys(payload, _MATCH_DATA_KEYS, url, what="match data")
        rosters = payload["rosters"]
        if not isinstance(rosters, dict) or not set(_ROSTER_SIDES).issubset(rosters):
            raise SchemaError(f"{url} match data 'rosters' missing h/a sides: {rosters!r}")
        return response.body

    def artifact_for_league_data(self, body: bytes, season: Season) -> RawArtifact:
        return RawArtifact(
            source=self.SOURCE,
            endpoint="league_data",
            season=season,
            url=self.league_data_url(season),
            http_status=200,
            body=body,
            fetched_at=datetime.now(UTC),
            connector_version=self.VERSION,
            params={"league": LEAGUE, "season": season.start_year},
            content_type="json",
        )

    def artifact_for_match_data(self, body: bytes, match_id: int, season: Season) -> RawArtifact:
        """Wrap one match's roster payload. Callers batch several of these
        into one :func:`fpl.storage.raw_io.write_chunk` call rather than
        writing one raw partition per match (plan §7.11 Finding: ~380
        matches/season would otherwise create thousands of tiny partitions)."""
        return RawArtifact(
            source=self.SOURCE,
            endpoint="match_data",
            season=season,
            url=self.match_data_url(match_id),
            http_status=200,
            body=body,
            fetched_at=datetime.now(UTC),
            connector_version=self.VERSION,
            params={"match_id": match_id},
            content_type="json",
        )


def _parse_json(body: bytes, url: str) -> Any:
    import json

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{url} did not return valid JSON: {exc}") from exc


def _check_keys(payload: Any, keys: tuple[str, ...], url: str, *, what: str) -> None:
    if not isinstance(payload, dict) or not set(keys).issubset(payload):
        raise SchemaError(f"{url} did not return the expected Understat {what} shape: {payload!r}")
