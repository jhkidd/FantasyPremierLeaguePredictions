from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from fpl.config import Season
from fpl.sources.errors import SchemaError
from fpl.sources.fetcher import HttpFetcher
from fpl.sources.fpl_api import FplApiConnector

BASE = "https://fpl.test/api"
SEASON = Season(2026)
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fpl"


def load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def connector() -> FplApiConnector:
    from fpl.config import SourceConfig

    fetcher = HttpFetcher(
        SourceConfig("test", min_request_interval=0.0, timeout=1.0, max_attempts=1),
        user_agent="test-agent",
        sleep=lambda _s: None,
    )
    return FplApiConnector(SEASON, fetcher=fetcher, base_url=BASE)


class TestBootstrapStatic:
    @respx.mock
    def test_returns_an_artifact(self, connector: FplApiConnector) -> None:
        respx.get(f"{BASE}/bootstrap-static/").mock(
            return_value=httpx.Response(200, json=load("bootstrap_static"))
        )
        artifact = connector.bootstrap_static()

        assert artifact.source == "fpl"
        assert artifact.endpoint == "bootstrap_static"
        assert artifact.season == SEASON
        assert artifact.http_status == 200
        assert artifact.connector_version == FplApiConnector.VERSION

    @respx.mock
    def test_body_is_the_untouched_response(self, connector: FplApiConnector) -> None:
        """Raw means raw: staging re-parses these bytes, so anything lost here
        is lost for good."""
        payload = json.dumps(load("bootstrap_static")).encode()
        respx.get(f"{BASE}/bootstrap-static/").mock(
            return_value=httpx.Response(200, content=payload)
        )
        assert connector.bootstrap_static().body == payload

    @respx.mock
    def test_fetched_at_is_timezone_aware(self, connector: FplApiConnector) -> None:
        respx.get(f"{BASE}/bootstrap-static/").mock(
            return_value=httpx.Response(200, json=load("bootstrap_static"))
        )
        assert connector.bootstrap_static().fetched_at.tzinfo is not None

    @respx.mock
    def test_recorded_payload_still_carries_the_defensive_fields(
        self, connector: FplApiConnector
    ) -> None:
        """These exist only from 2025/26 and are the sole free source of the
        defensive-contribution inputs. If FPL ever drops them, this fails."""
        payload = load("bootstrap_static")
        player = payload["elements"][0]
        for field in (
            "clearances_blocks_interceptions",
            "tackles",
            "recoveries",
            "defensive_contribution",
        ):
            assert field in player, field

    @respx.mock
    def test_empty_elements_is_a_schema_error(self, connector: FplApiConnector) -> None:
        """A 200 carrying nothing is how the API behaves between seasons.
        Storing it would look like every player vanished."""
        respx.get(f"{BASE}/bootstrap-static/").mock(
            return_value=httpx.Response(200, json={"elements": [], "teams": [], "events": []})
        )
        with pytest.raises(SchemaError, match="empty"):
            connector.bootstrap_static()

    @respx.mock
    def test_missing_key_is_a_schema_error(self, connector: FplApiConnector) -> None:
        respx.get(f"{BASE}/bootstrap-static/").mock(
            return_value=httpx.Response(200, json={"elements": [{"id": 1}], "teams": [{"id": 1}]})
        )
        with pytest.raises(SchemaError, match="events"):
            connector.bootstrap_static()

    @respx.mock
    def test_non_json_is_a_schema_error(self, connector: FplApiConnector) -> None:
        """Cloudflare interstitials arrive as HTML with a 200."""
        respx.get(f"{BASE}/bootstrap-static/").mock(
            return_value=httpx.Response(200, content=b"<html>Just a moment...</html>")
        )
        with pytest.raises(SchemaError, match="did not return JSON"):
            connector.bootstrap_static()

    @respx.mock
    def test_a_list_response_is_a_schema_error(self, connector: FplApiConnector) -> None:
        respx.get(f"{BASE}/bootstrap-static/").mock(return_value=httpx.Response(200, json=[]))
        with pytest.raises(SchemaError, match="should return an object"):
            connector.bootstrap_static()


class TestFixtures:
    @respx.mock
    def test_returns_an_artifact(self, connector: FplApiConnector) -> None:
        respx.get(f"{BASE}/fixtures/").mock(return_value=httpx.Response(200, json=load("fixtures")))
        artifact = connector.fixtures()
        assert artifact.endpoint == "fixtures"
        assert artifact.event is None

    @respx.mock
    def test_event_filter_is_passed_and_recorded(self, connector: FplApiConnector) -> None:
        route = respx.get(f"{BASE}/fixtures/").mock(
            return_value=httpx.Response(200, json=load("fixtures"))
        )
        artifact = connector.fixtures(event=7)
        assert route.calls[0].request.url.params["event"] == "7"
        assert artifact.event == 7
        assert artifact.params == {"event": 7}

    @respx.mock
    def test_empty_fixture_list_is_allowed(self, connector: FplApiConnector) -> None:
        """A gameweek with no fixtures is a blank, which is real and legal."""
        respx.get(f"{BASE}/fixtures/").mock(return_value=httpx.Response(200, json=[]))
        assert connector.fixtures(event=33).http_status == 200

    @respx.mock
    def test_object_response_is_a_schema_error(self, connector: FplApiConnector) -> None:
        respx.get(f"{BASE}/fixtures/").mock(return_value=httpx.Response(200, json={"a": 1}))
        with pytest.raises(SchemaError, match="should be a list"):
            connector.fixtures()


class TestEventLive:
    @respx.mock
    def test_returns_an_artifact_tagged_with_its_event(self, connector: FplApiConnector) -> None:
        respx.get(f"{BASE}/event/7/live/").mock(
            return_value=httpx.Response(200, json={"elements": [{"id": 1, "stats": {}}]})
        )
        artifact = connector.event_live(7)
        assert artifact.endpoint == "event_live"
        assert artifact.event == 7

    @respx.mock
    def test_preseason_empty_response_is_accepted(self, connector: FplApiConnector) -> None:
        """Before the season starts this returns `{"elements": []}` every day.
        A scheduled job meets it constantly, so it must not be an error."""
        respx.get(f"{BASE}/event/1/live/").mock(
            return_value=httpx.Response(200, json=load("event_live_preseason"))
        )
        assert connector.event_live(1).http_status == 200

    @respx.mock
    def test_missing_elements_key_is_a_schema_error(self, connector: FplApiConnector) -> None:
        respx.get(f"{BASE}/event/7/live/").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(SchemaError, match="elements"):
            connector.event_live(7)


class TestElementSummary:
    @respx.mock
    def test_returns_an_artifact(self, connector: FplApiConnector) -> None:
        respx.get(f"{BASE}/element-summary/42/").mock(
            return_value=httpx.Response(
                200, json={"history": [], "history_past": [], "fixtures": []}
            )
        )
        artifact = connector.element_summary(42)
        assert artifact.endpoint == "element_summary"
        assert artifact.params == {"player_id": 42}

    @respx.mock
    def test_missing_history_is_a_schema_error(self, connector: FplApiConnector) -> None:
        respx.get(f"{BASE}/element-summary/42/").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(SchemaError, match="history"):
            connector.element_summary(42)


class TestRecordedFixtures:
    """Guards on the recorded payloads themselves.

    They are the stand-in for the real API in every other test, so if they rot
    the whole offline suite quietly stops proving anything.
    """

    def test_bootstrap_keeps_every_top_level_key(self) -> None:
        payload = load("bootstrap_static")
        for key in ("elements", "teams", "events", "element_types", "game_settings", "phases"):
            assert key in payload, key

    def test_bootstrap_keeps_all_38_events(self) -> None:
        """Phase 3's capture-window logic is only worth testing against a
        realistic calendar."""
        assert len(load("bootstrap_static")["events"]) == 38

    def test_events_carry_the_fields_capture_scheduling_depends_on(self) -> None:
        event = load("bootstrap_static")["events"][0]
        for field in ("id", "deadline_time", "finished", "data_checked"):
            assert field in event, field

    def test_fixtures_carry_kickoff_time(self) -> None:
        """Point-in-time correctness filters on kickoff, not full time."""
        assert "kickoff_time" in load("fixtures")[0]

    def test_fixtures_are_trimmed(self) -> None:
        assert len(load("fixtures")) <= 10
