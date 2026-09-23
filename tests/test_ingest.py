from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from fpl.config import Season, SourceConfig
from fpl.ingest import ROUTINE_ENDPOINTS, ingest_fpl, missing_event_live_captures
from fpl.sources.errors import BlockedError
from fpl.sources.fetcher import HttpFetcher
from fpl.sources.fpl_api import FplApiConnector
from fpl.storage import paths
from fpl.storage.raw_io import read_raw

BASE = "https://fpl.test/api"
SEASON = Season(2026)
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "fpl"


def load(name: str):
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def connector() -> FplApiConnector:
    fetcher = HttpFetcher(
        SourceConfig("test", min_request_interval=0.0, timeout=1.0, max_attempts=1),
        user_agent="test-agent",
        sleep=lambda _s: None,
    )
    return FplApiConnector(SEASON, fetcher=fetcher, base_url=BASE)


def mock_routine() -> None:
    respx.get(f"{BASE}/bootstrap-static/").mock(
        return_value=httpx.Response(200, json=load("bootstrap_static"))
    )
    respx.get(f"{BASE}/fixtures/").mock(return_value=httpx.Response(200, json=load("fixtures")))


class TestRoutineIngestion:
    @respx.mock
    def test_default_pulls_the_routine_set(self, connector: FplApiConnector) -> None:
        mock_routine()
        results = ingest_fpl(SEASON, connector=connector)
        assert len(results) == len(ROUTINE_ENDPOINTS) - 1
        assert all(result.written for result in results)

    @respx.mock
    def test_no_team_configured_still_pulls_prices_and_fixtures(
        self, connector: FplApiConnector
    ) -> None:
        """Prices and fixtures are why the daily job exists and do not depend
        on a team, so a missing entry ID must not fail the snapshot."""
        mock_routine()
        results = ingest_fpl(SEASON, connector=connector, entry_id=None)
        assert [result.path.parts[-3] for result in results] == [
            "bootstrap_static",
            "fixtures",
        ]

    @respx.mock
    def test_a_configured_team_is_snapshotted_daily(self, connector: FplApiConnector) -> None:
        """Bank, squad value and overall rank are published only as a current
        value, so a day not captured is a day gone."""
        mock_routine()
        respx.get(f"{BASE}/entry/2282251/").mock(
            return_value=httpx.Response(200, json={"id": 2282251, "last_deadline_bank": 5})
        )
        results = ingest_fpl(SEASON, connector=connector, entry_id=2282251)
        assert len(results) == len(ROUTINE_ENDPOINTS)
        assert any(result.path.parts[-3] == "entry" for result in results)

    @respx.mock
    def test_asking_for_entry_without_a_team_is_an_error(self, connector: FplApiConnector) -> None:
        with pytest.raises(ValueError, match="entry requires"):
            ingest_fpl(SEASON, ["entry"], connector=connector)

    @respx.mock
    def test_writes_readable_artifacts(self, connector: FplApiConnector) -> None:
        mock_routine()
        results = ingest_fpl(SEASON, connector=connector)
        body, meta = read_raw(results[0].path)
        assert json.loads(body)["teams"]
        assert meta["source"] == "fpl"

    @respx.mock
    def test_is_idempotent(self, connector: FplApiConnector) -> None:
        """A scheduled job runs every day whether or not anything changed."""
        mock_routine()
        ingest_fpl(SEASON, connector=connector)
        second = ingest_fpl(SEASON, connector=connector)
        assert not any(result.written for result in second)

    @respx.mock
    def test_force_overrides_content_addressing(self, connector: FplApiConnector) -> None:
        mock_routine()
        ingest_fpl(SEASON, connector=connector)
        second = ingest_fpl(SEASON, connector=connector, force=True)
        assert all(result.written for result in second)

    @respx.mock
    def test_respects_an_explicit_data_root(
        self, connector: FplApiConnector, tmp_path: Path
    ) -> None:
        mock_routine()
        root = tmp_path / "elsewhere"
        results = ingest_fpl(SEASON, connector=connector, data_root=root)
        assert all(result.path.is_relative_to(root) for result in results)


class TestEndpointSelection:
    @respx.mock
    def test_single_endpoint(self, connector: FplApiConnector) -> None:
        mock_routine()
        results = ingest_fpl(SEASON, ["bootstrap-static"], connector=connector)
        assert len(results) == 1
        assert results[0].path.is_relative_to(
            paths.raw_endpoint_dir("fpl", "bootstrap_static", SEASON)
        )

    @respx.mock
    def test_event_live_with_no_bootstrap_capture_yet_fetches_nothing(
        self, connector: FplApiConnector
    ) -> None:
        """Auto-discovery needs a bootstrap-static capture on disk to know
        which gameweeks are finalised; none yet is a clean no-op, not an
        error (close-live-ingestion-gap.md)."""
        assert ingest_fpl(SEASON, ["event-live"], connector=connector) == []

    @respx.mock
    def test_element_summary_requires_a_player(self, connector: FplApiConnector) -> None:
        with pytest.raises(ValueError, match="requires --player"):
            ingest_fpl(SEASON, ["element-summary"], connector=connector)

    @respx.mock
    def test_event_live_partitions_by_event(self, connector: FplApiConnector) -> None:
        respx.get(f"{BASE}/event/7/live/").mock(
            return_value=httpx.Response(200, json={"elements": [{"id": 1}]})
        )
        results = ingest_fpl(SEASON, ["event-live"], event=7, connector=connector)
        assert "event=7" in str(results[0].path)

    def test_unknown_endpoint_names_the_alternatives(self, connector: FplApiConnector) -> None:
        with pytest.raises(ValueError, match="supported:"):
            ingest_fpl(SEASON, ["nonesuch"], connector=connector)

    def test_unknown_endpoint_is_rejected_before_any_fetch(
        self, connector: FplApiConnector
    ) -> None:
        """Validation happens up front so a typo in a workflow never results in
        a half-completed multi-endpoint pull."""
        with respx.mock:
            route = respx.get(f"{BASE}/bootstrap-static/").mock(
                return_value=httpx.Response(200, json=load("bootstrap_static"))
            )
            with pytest.raises(ValueError):
                ingest_fpl(SEASON, ["bootstrap-static", "nonesuch"], connector=connector)
            assert route.call_count == 0


class TestEventLiveAutoDiscovery:
    """``event-live`` with no ``--event`` auto-discovers finished, finalised
    gameweeks not yet captured — the same mechanism serves both the nightly
    steady-state case and a full-season backfill (close-live-ingestion-gap.md).
    """

    @staticmethod
    def _bootstrap(events: list[dict]) -> dict:
        return {"elements": [{"id": 1}], "teams": [{"id": 1}], "events": events}

    @respx.mock
    def test_fetches_finished_and_checked_events_only(self, connector: FplApiConnector) -> None:
        payload = self._bootstrap(
            [
                {"id": 1, "finished": True, "data_checked": True},
                {"id": 2, "finished": True, "data_checked": False},
                {"id": 3, "finished": False, "data_checked": False},
            ]
        )
        respx.get(f"{BASE}/bootstrap-static/").mock(return_value=httpx.Response(200, json=payload))
        ingest_fpl(SEASON, ["bootstrap-static"], connector=connector)

        route = respx.get(f"{BASE}/event/1/live/").mock(
            return_value=httpx.Response(200, json={"elements": [{"id": 1}]})
        )
        results = ingest_fpl(SEASON, ["event-live"], connector=connector)

        assert route.call_count == 1
        assert len(results) == 1
        assert "event=1" in str(results[0].path)

    @respx.mock
    def test_an_already_captured_event_is_not_refetched(self, connector: FplApiConnector) -> None:
        payload = self._bootstrap([{"id": 1, "finished": True, "data_checked": True}])
        respx.get(f"{BASE}/bootstrap-static/").mock(return_value=httpx.Response(200, json=payload))
        ingest_fpl(SEASON, ["bootstrap-static"], connector=connector)
        respx.get(f"{BASE}/event/1/live/").mock(
            return_value=httpx.Response(200, json={"elements": [{"id": 1}]})
        )
        ingest_fpl(SEASON, ["event-live"], connector=connector)

        results = ingest_fpl(SEASON, ["event-live"], connector=connector)
        assert results == []

    @respx.mock
    def test_missing_event_live_captures_helper(self, connector: FplApiConnector) -> None:
        payload = self._bootstrap(
            [
                {"id": 1, "finished": True, "data_checked": True},
                {"id": 2, "finished": True, "data_checked": True},
            ]
        )
        respx.get(f"{BASE}/bootstrap-static/").mock(return_value=httpx.Response(200, json=payload))
        ingest_fpl(SEASON, ["bootstrap-static"], connector=connector)

        assert missing_event_live_captures(SEASON) == [1, 2]


class TestFailures:
    @respx.mock
    def test_blocked_propagates(self, connector: FplApiConnector) -> None:
        respx.get(f"{BASE}/bootstrap-static/").mock(return_value=httpx.Response(403))
        with pytest.raises(BlockedError):
            ingest_fpl(SEASON, ["bootstrap-static"], connector=connector)

    @respx.mock
    def test_a_failed_fetch_writes_nothing(self, connector: FplApiConnector) -> None:
        """Partial state is worse than no state: the next run must start clean."""
        respx.get(f"{BASE}/bootstrap-static/").mock(return_value=httpx.Response(403))
        with pytest.raises(BlockedError):
            ingest_fpl(SEASON, ["bootstrap-static"], connector=connector)
        assert paths.latest_partition("fpl", "bootstrap_static", SEASON) is None

    @respx.mock
    def test_earlier_endpoints_survive_a_later_failure(self, connector: FplApiConnector) -> None:
        """Append-only means a partial run leaves a gap, not a corruption."""
        respx.get(f"{BASE}/bootstrap-static/").mock(
            return_value=httpx.Response(200, json=load("bootstrap_static"))
        )
        respx.get(f"{BASE}/fixtures/").mock(return_value=httpx.Response(403))
        with pytest.raises(BlockedError):
            ingest_fpl(SEASON, ["bootstrap-static", "fixtures"], connector=connector)
        assert paths.latest_partition("fpl", "bootstrap_static", SEASON) is not None
