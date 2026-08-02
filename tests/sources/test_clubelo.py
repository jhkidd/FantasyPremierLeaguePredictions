from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from fpl.config import Season, SourceConfig
from fpl.sources.clubelo import RATINGS_CSV_HEADER, ClubEloConnector
from fpl.sources.errors import SchemaError
from fpl.sources.fetcher import HttpFetcher

BASE = "https://clubelo.test"
SEASON = Season(2026)
AS_OF = date(2026, 8, 15)

RATINGS_CSV = (
    "Rank,Club,Country,Level,Elo,From,To\n"
    "1,Arsenal,ENG,1,2063.7578125,2026-05-31,2026-08-21\n"
    "2,Man City,ENG,1,2029.451171875,2026-05-31,2026-08-21\n"
)


@pytest.fixture
def connector() -> ClubEloConnector:
    fetcher = HttpFetcher(
        SourceConfig("test", min_request_interval=0.0, timeout=1.0, max_attempts=1),
        user_agent="test-agent",
        sleep=lambda _s: None,
    )
    return ClubEloConnector(fetcher=fetcher, base_url=BASE)


class TestFetchRatings:
    @respx.mock
    def test_fetches_the_date_keyed_endpoint(self, connector: ClubEloConnector) -> None:
        respx.get(f"{BASE}/{AS_OF.isoformat()}").mock(
            return_value=httpx.Response(200, text=RATINGS_CSV)
        )
        body = connector.fetch_ratings(AS_OF)
        assert body == RATINGS_CSV.encode()

    @respx.mock
    def test_unexpected_header_is_a_schema_error(self, connector: ClubEloConnector) -> None:
        """Guards against a silently changed CSV shape (plan §7.2 Finding) —
        the ratings and Fixtures endpoints are structurally different, so a
        header mismatch here must fail loudly rather than stage garbage."""
        respx.get(f"{BASE}/{AS_OF.isoformat()}").mock(
            return_value=httpx.Response(
                200, text="Date,Country,Home,Away,GD<-5\n2026-08-15,ENG,A,B,0.1\n"
            )
        )
        with pytest.raises(SchemaError):
            connector.fetch_ratings(AS_OF)

    @respx.mock
    def test_confirmed_live_header_matches_the_constant(self, connector: ClubEloConnector) -> None:
        """RATINGS_CSV_HEADER was confirmed against the live endpoint during
        phase 7 probing; this pins that shape so a future upstream change is
        caught rather than silently accepted."""
        respx.get(f"{BASE}/{AS_OF.isoformat()}").mock(
            return_value=httpx.Response(200, text=RATINGS_CSV)
        )
        connector.fetch_ratings(AS_OF)
        assert RATINGS_CSV.splitlines()[0] == RATINGS_CSV_HEADER


class TestArtifactForRatings:
    def test_wraps_the_body_with_the_caller_supplied_season(
        self, connector: ClubEloConnector
    ) -> None:
        artifact = connector.artifact_for_ratings(RATINGS_CSV.encode(), AS_OF, SEASON)

        assert artifact.source == "clubelo"
        assert artifact.endpoint == "ratings"
        assert artifact.season == SEASON
        assert artifact.body == RATINGS_CSV.encode()
        assert artifact.http_status == 200
        assert artifact.connector_version == ClubEloConnector.VERSION
        assert artifact.content_type == "csv"
        assert artifact.params == {"date": AS_OF.isoformat()}

    def test_fetched_at_is_timezone_aware(self, connector: ClubEloConnector) -> None:
        artifact = connector.artifact_for_ratings(RATINGS_CSV.encode(), AS_OF, SEASON)
        assert artifact.fetched_at.tzinfo is not None

    def test_url_carries_the_requested_date(self, connector: ClubEloConnector) -> None:
        """The connector never derives its own T-1 offset (plan §7.2) — the
        date it is given is the date it records, so the caller's
        leakage-avoidance choice stays visible in the stored artifact."""
        artifact = connector.artifact_for_ratings(RATINGS_CSV.encode(), AS_OF, SEASON)
        assert artifact.url.endswith(AS_OF.isoformat())
