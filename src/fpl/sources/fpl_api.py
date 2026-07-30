"""Connector for the official FPL API.

The API is undocumented and unversioned, so this module deliberately does as
little as possible: fetch bytes, check they are not obviously nonsense, and hand
them to storage exactly as received. Every interpretation happens later, where
it can be re-run from the raw bytes without re-fetching.

No authentication is required for any endpoint used here (spec §13).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fpl.config import Config, Season
from fpl.sources.errors import SchemaError
from fpl.sources.fetcher import FetchResponse, HttpFetcher
from fpl.storage.raw_io import RawArtifact

__all__ = ["BASE_URL", "OVERALL_LEAGUE_ID", "FplApiConnector"]

BASE_URL = "https://fantasy.premierleague.com/api"

OVERALL_LEAGUE_ID = 314
"""The 'Overall' classic league every entry belongs to."""


class FplApiConnector:
    """Fetches raw payloads from the FPL API.

    ``VERSION`` is recorded in every artifact's metadata. Bump it when the
    fetching behaviour changes in a way that could affect what lands on disk,
    so a future reader can tell which code produced which bytes.
    """

    VERSION = "1"
    SOURCE = "fpl"

    def __init__(
        self,
        season: Season,
        *,
        fetcher: HttpFetcher | None = None,
        config: Config | None = None,
        base_url: str = BASE_URL,
        source_profile: str = "fpl",
    ) -> None:
        self.season = season
        self.base_url = base_url.rstrip("/")
        loaded = config or Config.load()
        self._owns_fetcher = fetcher is None
        self.fetcher = fetcher or HttpFetcher(loaded.source(source_profile), loaded.user_agent)

    def __enter__(self) -> FplApiConnector:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_fetcher:
            self.fetcher.close()

    # -- endpoints -------------------------------------------------------

    def bootstrap_static(self) -> RawArtifact:
        """Players, teams, events and the current game state.

        The single most important endpoint: it is the only source of prices,
        ownership, availability news and the defensive-contribution stats.
        """
        response = self.fetcher.get(f"{self.base_url}/bootstrap-static/")
        payload = self._parse(response)
        _require_non_empty_lists(payload, ("elements", "teams", "events"), response.url)
        return self._artifact("bootstrap_static", response)

    def fixtures(self, *, event: int | None = None) -> RawArtifact:
        params = {"event": event} if event is not None else None
        response = self.fetcher.get(f"{self.base_url}/fixtures/", params=params)
        payload = self._parse(response)
        if not isinstance(payload, list):
            raise SchemaError(f"fixtures should be a list, got {type(payload).__name__}")
        return self._artifact("fixtures", response, params=params or {}, event=event)

    def event_live(self, event: int) -> RawArtifact:
        """Per-player stats for one gameweek. One request covers every player."""
        response = self.fetcher.get(f"{self.base_url}/event/{event}/live/")
        payload = self._parse(response)
        if not isinstance(payload, Mapping) or "elements" not in payload:
            raise SchemaError(f"event/{event}/live should carry an 'elements' key")
        return self._artifact("event_live", response, event=event)

    def element_summary(self, player_id: int) -> RawArtifact:
        """One player's fixture history. Expensive in aggregate — backfill only."""
        response = self.fetcher.get(f"{self.base_url}/element-summary/{player_id}/")
        payload = self._parse(response)
        if not isinstance(payload, Mapping) or "history" not in payload:
            raise SchemaError(f"element-summary/{player_id} should carry a 'history' key")
        return self._artifact("element_summary", response, params={"player_id": player_id})

    # -- manager endpoints (spec §6.1) -----------------------------------

    def classic_league_standings(self, league_id: int, page: int = 1) -> RawArtifact:
        """One page of a classic league's table, 50 entries per page.

        Empty results are *not* a schema error. The overall league carries no
        standings until a gameweek has been scored, and a mini-league carries
        none until its members join, so both legitimately return zero rows.
        Emptiness is a fact for the caller to act on, not a malformed response.
        """
        params = {"page_standings": page}
        response = self.fetcher.get(
            f"{self.base_url}/leagues-classic/{league_id}/standings/", params=params
        )
        payload = self._parse(response)
        if not isinstance(payload, Mapping) or "standings" not in payload:
            raise SchemaError(f"leagues-classic/{league_id} should carry a 'standings' key")
        if not isinstance(payload["standings"], Mapping):
            raise SchemaError(f"leagues-classic/{league_id} 'standings' should be an object")
        return self._artifact(
            "league_standings", response, params={"league_id": league_id, "page": page}
        )

    def entry(self, entry_id: int) -> RawArtifact:
        """One manager's profile. Carries `leagues`, which is how a configured
        mini-league is discovered from its numeric ID rather than its join code."""
        response = self.fetcher.get(f"{self.base_url}/entry/{entry_id}/")
        payload = self._parse(response)
        if not isinstance(payload, Mapping) or "id" not in payload:
            raise SchemaError(f"entry/{entry_id} should carry an 'id' key")
        return self._artifact("entry", response, params={"entry_id": entry_id})

    def entry_picks(self, entry_id: int, event: int) -> RawArtifact:
        """One manager's squad for one gameweek.

        The perishable endpoint: FPL does not retain picks across seasons, and
        no archive reconstructs them (spec §6.1).
        """
        response = self.fetcher.get(f"{self.base_url}/entry/{entry_id}/event/{event}/picks/")
        payload = self._parse(response)
        if not isinstance(payload, Mapping) or "picks" not in payload:
            raise SchemaError(f"entry/{entry_id}/event/{event}/picks should carry a 'picks' key")
        if not isinstance(payload["picks"], list) or not payload["picks"]:
            raise SchemaError(f"entry/{entry_id}/event/{event}/picks returned no picks")
        return self._artifact("entry_picks", response, params={"entry_id": entry_id}, event=event)

    # -- helpers ---------------------------------------------------------

    def _parse(self, response: FetchResponse) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise SchemaError(f"{response.url} did not return JSON: {exc}") from exc

    def _artifact(
        self,
        endpoint: str,
        response: FetchResponse,
        *,
        params: Mapping[str, Any] | None = None,
        event: int | None = None,
    ) -> RawArtifact:
        return RawArtifact(
            source=self.SOURCE,
            endpoint=endpoint,
            season=self.season,
            url=response.url,
            http_status=response.status,
            body=response.body,
            fetched_at=datetime.now(UTC),
            connector_version=self.VERSION,
            params=dict(params or {}),
            event=event,
        )


def _require_non_empty_lists(payload: Any, keys: tuple[str, ...], url: str) -> None:
    """A shallow sanity check, not a schema.

    Enough to catch an empty or error payload dressed up as a 200 — which the
    FPL API does return between seasons — without pinning down field names that
    legitimately change year to year. Precise schemas arrive in phase 4.
    """
    if not isinstance(payload, Mapping):
        raise SchemaError(f"{url} should return an object, got {type(payload).__name__}")
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, list):
            raise SchemaError(f"{url} is missing list field {key!r}")
        if not value:
            raise SchemaError(f"{url} returned an empty {key!r}; the season may not have started")
