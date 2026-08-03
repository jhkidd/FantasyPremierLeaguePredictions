from __future__ import annotations

import httpx
import pytest
import respx

from fpl.config import Season, SourceConfig
from fpl.sources.errors import SchemaError
from fpl.sources.fetcher import HttpFetcher
from fpl.sources.footballdata import FootballDataConnector, url_for_season

BASE = "https://footballdata.test"
SEASON = Season(2025)

# Trimmed excerpt of the live mmz4281/2526/E0.csv, confirmed during phase 7
# probing: real header, two real rows.
MATCH_CSV = (
    "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HTHG,HTAG,HTR,Referee,B365H,B365D,B365A\n"
    "E0,15/08/2025,20:00,Liverpool,Bournemouth,4,2,H,1,0,H,A Taylor,1.3,6,8.5\n"
    "E0,16/08/2025,12:30,Aston Villa,Newcastle,0,0,D,0,0,D,C Pawson,2.25,3.5,2.9\n"
)


@pytest.fixture
def connector() -> FootballDataConnector:
    fetcher = HttpFetcher(
        SourceConfig("test", min_request_interval=0.0, timeout=1.0, max_attempts=1),
        user_agent="test-agent",
        sleep=lambda _s: None,
    )
    return FootballDataConnector(fetcher=fetcher, base_url=BASE)


class TestUrlForSeason:
    def test_builds_the_two_digit_year_pair_path(self) -> None:
        assert url_for_season(Season(2025), base_url=BASE) == f"{BASE}/mmz4281/2526/E0.csv"

    def test_handles_a_century_rollover(self) -> None:
        assert url_for_season(Season(2099), base_url=BASE) == f"{BASE}/mmz4281/9900/E0.csv"


class TestFetchSeason:
    @respx.mock
    def test_fetches_the_season_csv(self, connector: FootballDataConnector) -> None:
        respx.get(f"{BASE}/mmz4281/2526/E0.csv").mock(
            return_value=httpx.Response(200, text=MATCH_CSV)
        )
        assert connector.fetch_season(SEASON) == MATCH_CSV.encode()

    @respx.mock
    def test_unexpected_header_is_a_schema_error(self, connector: FootballDataConnector) -> None:
        respx.get(f"{BASE}/mmz4281/2526/E0.csv").mock(
            return_value=httpx.Response(200, text="<html>not found</html>")
        )
        with pytest.raises(SchemaError):
            connector.fetch_season(SEASON)


class TestArtifactForSeason:
    def test_wraps_the_body_for_one_season(self, connector: FootballDataConnector) -> None:
        artifact = connector.artifact_for_season(MATCH_CSV.encode(), SEASON)

        assert artifact.source == "footballdata"
        assert artifact.endpoint == "matches_and_odds"
        assert artifact.season == SEASON
        assert artifact.body == MATCH_CSV.encode()
        assert artifact.content_type == "csv"
        assert artifact.connector_version == FootballDataConnector.VERSION
        assert artifact.url.endswith("mmz4281/2526/E0.csv")

    def test_fetched_at_is_timezone_aware(self, connector: FootballDataConnector) -> None:
        artifact = connector.artifact_for_season(MATCH_CSV.encode(), SEASON)
        assert artifact.fetched_at.tzinfo is not None
