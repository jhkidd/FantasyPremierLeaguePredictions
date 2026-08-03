from __future__ import annotations

import json

import httpx
import pytest
import respx

from fpl.config import Season, SourceConfig
from fpl.sources.errors import SchemaError
from fpl.sources.fetcher import HttpFetcher
from fpl.sources.understat import LEAGUE, UnderstatConnector

BASE = "https://understat.test"
SEASON = Season(2025)

LEAGUE_DATA = {
    "teams": {"1": {"title": "Manchester United"}},
    "players": [{"id": "1", "player_name": "Bruno Fernandes", "team_title": "Manchester United"}],
    "dates": [{"id": "1", "isResult": True, "h": {"title": "Manchester United"}}],
}

MATCH_DATA = {
    "rosters": {
        "h": {"1": {"player": "Bruno Fernandes", "player_id": "1", "xG": "0.4", "time": "90"}},
        "a": {"2": {"player": "Someone Else", "player_id": "2", "xG": "0.1", "time": "90"}},
    },
    "shots": {},
}


@pytest.fixture
def connector() -> UnderstatConnector:
    fetcher = HttpFetcher(
        SourceConfig("test", min_request_interval=0.0, timeout=1.0, max_attempts=1),
        user_agent="test-agent",
        sleep=lambda _s: None,
    )
    return UnderstatConnector(fetcher=fetcher, base_url=BASE)


class TestFetchLeagueData:
    @respx.mock
    def test_fetches_the_league_and_season_keyed_endpoint(
        self, connector: UnderstatConnector
    ) -> None:
        route = respx.get(f"{BASE}/getLeagueData/{LEAGUE}/{SEASON.start_year}").mock(
            return_value=httpx.Response(200, json=LEAGUE_DATA)
        )
        body = connector.fetch_league_data(SEASON)
        assert json.loads(body) == LEAGUE_DATA
        assert route.calls.last.request.headers["X-Requested-With"] == "XMLHttpRequest"

    @respx.mock
    def test_missing_ajax_header_looks_like_a_404_not_this_test(
        self, connector: UnderstatConnector
    ) -> None:
        """Documents the live finding: Understat 404s a request lacking the
        AJAX header, so the header must always be sent, never optional."""
        respx.get(f"{BASE}/getLeagueData/{LEAGUE}/{SEASON.start_year}").mock(
            return_value=httpx.Response(200, json=LEAGUE_DATA)
        )
        connector.fetch_league_data(SEASON)  # would raise if the header were dropped

    @respx.mock
    def test_missing_expected_keys_is_a_schema_error(self, connector: UnderstatConnector) -> None:
        respx.get(f"{BASE}/getLeagueData/{LEAGUE}/{SEASON.start_year}").mock(
            return_value=httpx.Response(200, json={"unexpected": True})
        )
        with pytest.raises(SchemaError):
            connector.fetch_league_data(SEASON)

    @respx.mock
    def test_non_json_body_is_a_schema_error(self, connector: UnderstatConnector) -> None:
        respx.get(f"{BASE}/getLeagueData/{LEAGUE}/{SEASON.start_year}").mock(
            return_value=httpx.Response(200, text="<html>not json</html>")
        )
        with pytest.raises(SchemaError):
            connector.fetch_league_data(SEASON)


class TestFetchMatchData:
    @respx.mock
    def test_fetches_the_match_keyed_endpoint(self, connector: UnderstatConnector) -> None:
        route = respx.get(f"{BASE}/getMatchData/12345").mock(
            return_value=httpx.Response(200, json=MATCH_DATA)
        )
        body = connector.fetch_match_data(12345)
        assert json.loads(body) == MATCH_DATA
        assert route.calls.last.request.headers["X-Requested-With"] == "XMLHttpRequest"

    @respx.mock
    def test_missing_rosters_is_a_schema_error(self, connector: UnderstatConnector) -> None:
        respx.get(f"{BASE}/getMatchData/12345").mock(
            return_value=httpx.Response(200, json={"shots": {}})
        )
        with pytest.raises(SchemaError):
            connector.fetch_match_data(12345)

    @respx.mock
    def test_rosters_missing_a_side_is_a_schema_error(self, connector: UnderstatConnector) -> None:
        respx.get(f"{BASE}/getMatchData/12345").mock(
            return_value=httpx.Response(200, json={"rosters": {"h": {}}})
        )
        with pytest.raises(SchemaError):
            connector.fetch_match_data(12345)


class TestArtifacts:
    def test_artifact_for_league_data(self, connector: UnderstatConnector) -> None:
        body = json.dumps(LEAGUE_DATA).encode()
        artifact = connector.artifact_for_league_data(body, SEASON)
        assert artifact.source == "understat"
        assert artifact.endpoint == "league_data"
        assert artifact.season == SEASON
        assert artifact.connector_version == UnderstatConnector.VERSION
        assert artifact.params == {"league": LEAGUE, "season": SEASON.start_year}
        assert artifact.fetched_at.tzinfo is not None

    def test_artifact_for_match_data(self, connector: UnderstatConnector) -> None:
        body = json.dumps(MATCH_DATA).encode()
        artifact = connector.artifact_for_match_data(body, 12345, SEASON)
        assert artifact.source == "understat"
        assert artifact.endpoint == "match_data"
        assert artifact.season == SEASON
        assert artifact.params == {"match_id": 12345}
        assert artifact.fetched_at.tzinfo is not None
