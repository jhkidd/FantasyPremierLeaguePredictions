"""Connector for the Club Elo API.

Free public REST API (spec §13/§18.2), **HTTP only** - ``api.clubelo.com``
does not respond on HTTPS at all, confirmed live during phase 7 probing
(plan Finding A). One endpoint is in scope:

- ``api.clubelo.com/{YYYY-MM-DD}``: every club's Elo rating as of that date,
  as one CSV covering every club Club Elo tracks (not just the Premier
  League) - filtering to the clubs we actually need happens downstream, at
  facts-assembly time, via the team crosswalk (plan §7.13), not here.

``api.clubelo.com/Fixtures`` was also probed live and turned out to expose a
full scoreline/goal-difference probability distribution
(``Date,Country,Home,Away,GD<-5,...,R:0-0,R:0-1,...``), not a simple
win/draw/loss breakdown as the design first assumed. Nothing in spec §18.5's
locked column set (``elo_rating``, ``opponent_elo_rating``) needs it: a
rating's ``From``/``To`` validity window already extends into the future
until the next match is played, so a forward-looking fixture's Elo can be
read from the same per-date endpoint. Deliberately not implemented - YAGNI -
until a future column set actually calls for match-outcome probabilities.

Ratings must be queried the day *before* kickoff, never the fixture's own
date (plan §7.2 - Elo updates same-day after a match is played, so querying
the fixture's own date risks the rating already reflecting that day's
result on early-kickoff days). That offset is the caller's decision
(facts/team_fixture assembly, plan §7.13), not this connector's - it fetches
exactly the date it is asked for and knows nothing about fixtures, keeping
the leakage-avoidance choice visible and testable at the call site.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fpl.config import Config, Season
from fpl.sources.errors import SchemaError
from fpl.sources.fetcher import HttpFetcher
from fpl.storage.raw_io import RawArtifact

__all__ = ["BASE_URL", "RATINGS_CSV_HEADER", "ClubEloConnector"]

BASE_URL = "http://api.clubelo.com"
"""HTTP, never HTTPS (spec §13) - confirmed live: ``api.clubelo.com`` does
not answer on TLS at all."""

RATINGS_CSV_HEADER = "Rank,Club,Country,Level,Elo,From,To"
"""Confirmed live during phase 7 probing (2026-08-02)."""


class ClubEloConnector:
    """Fetches one day's worth of Club Elo ratings at a time.

    ``VERSION`` follows the same convention as every other connector: bumped
    whenever the fetching logic changes in a way that could affect what
    lands on disk.
    """

    VERSION = "1"
    SOURCE = "clubelo"

    def __init__(
        self,
        *,
        fetcher: HttpFetcher | None = None,
        config: Config | None = None,
        base_url: str = BASE_URL,
        source_profile: str = "clubelo",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        loaded = config or Config.load()
        self._owns_fetcher = fetcher is None
        self.fetcher = fetcher or HttpFetcher(loaded.source(source_profile), loaded.user_agent)

    def __enter__(self) -> ClubEloConnector:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_fetcher:
            self.fetcher.close()

    def fetch_ratings(self, as_of_date: date) -> bytes:
        """Every club's Elo rating as of ``as_of_date``.

        The caller decides which date to ask for - this connector applies no
        leakage-avoidance offset of its own (plan §7.2), so that decision
        stays visible and testable at the call site rather than buried here.
        """
        response = self.fetcher.get(f"{self.base_url}/{as_of_date.isoformat()}")
        _check_ratings_header(response.body, response.url)
        return response.body

    def artifact_for_ratings(self, body: bytes, as_of_date: date, season: Season) -> RawArtifact:
        """Wrap one day's ratings CSV as a raw artifact, ready to write.

        ``season`` is supplied by the caller rather than derived from
        ``as_of_date`` here: the backfill orchestration already knows which
        season a fixture date belongs to (it is iterating that season's own
        fixtures), so a second, independent date-to-season inference would
        only be a second place for that logic to drift out of step.
        """
        return RawArtifact(
            source=self.SOURCE,
            endpoint="ratings",
            season=season,
            url=f"{self.base_url}/{as_of_date.isoformat()}",
            http_status=200,
            body=body,
            fetched_at=datetime.now(UTC),
            connector_version=self.VERSION,
            params={"date": as_of_date.isoformat()},
            content_type="csv",
        )


def _check_ratings_header(body: bytes, url: str) -> None:
    """A shallow sanity check: the response looks like Club Elo's ratings
    CSV, not an HTML error page or an empty body dressed up as a 200."""
    first_line = body.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    if first_line != RATINGS_CSV_HEADER:
        raise SchemaError(
            f"{url} did not return the expected Club Elo ratings header: {first_line!r}"
        )
