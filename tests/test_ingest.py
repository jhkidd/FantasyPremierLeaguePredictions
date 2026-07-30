from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from fpl.config import Season, SourceConfig
from fpl.ingest import ROUTINE_ENDPOINTS, ingest_fpl
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
        assert len(results) == len(ROUTINE_ENDPOINTS)
        assert all(result.written for result in results)

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
    def test_event_live_requires_an_event(self, connector: FplApiConnector) -> None:
        with pytest.raises(ValueError, match="requires --event"):
            ingest_fpl(SEASON, ["event-live"], connector=connector)

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
