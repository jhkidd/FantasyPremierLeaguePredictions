from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from fpl.config import Season, SourceConfig
from fpl.sources.fetcher import HttpFetcher
from fpl.sources.understat import LEAGUE, UnderstatConnector
from fpl.storage import paths
from fpl.storage.raw_io import read_raw, write_raw
from fpl.understat_capture import (
    CHUNK_SIZE,
    capture_league_data,
    capture_match_data,
)

BASE = "https://understat.test"
SEASON = Season(2025)

LEAGUE_DATA = {
    "teams": {},
    "players": [],
    "dates": [
        {"id": str(match_id), "isResult": True, "h": {"title": "A"}, "a": {"title": "B"}}
        for match_id in range(1, 4)
    ]
    + [{"id": "4", "isResult": False, "h": {"title": "A"}, "a": {"title": "C"}}],
}


def match_payload(match_id: int) -> dict:
    return {
        "rosters": {
            "h": {"1": {"player": "P1", "player_id": "1", "time": "90", "xG": "0.1"}},
            "a": {"2": {"player": "P2", "player_id": "2", "time": "90", "xG": "0.2"}},
        },
        "shots": {},
        "match_id": match_id,
    }


@pytest.fixture
def connector() -> UnderstatConnector:
    fetcher = HttpFetcher(
        SourceConfig("test", min_request_interval=0.0, timeout=1.0, max_attempts=1),
        user_agent="test-agent",
        sleep=lambda _s: None,
    )
    return UnderstatConnector(fetcher=fetcher, base_url=BASE)


class TestCaptureLeagueData:
    @respx.mock
    def test_writes_the_raw_artifact(self, connector: UnderstatConnector, tmp_path: Path) -> None:
        respx.get(f"{BASE}/getLeagueData/{LEAGUE}/{SEASON.start_year}").mock(
            return_value=httpx.Response(200, json=LEAGUE_DATA)
        )
        result = capture_league_data(SEASON, connector=connector, data_root=tmp_path)
        assert result.written is True
        body, _meta = read_raw(result.path)
        assert json.loads(body) == LEAGUE_DATA

    @respx.mock
    def test_unchanged_content_is_skipped(
        self, connector: UnderstatConnector, tmp_path: Path
    ) -> None:
        respx.get(f"{BASE}/getLeagueData/{LEAGUE}/{SEASON.start_year}").mock(
            return_value=httpx.Response(200, json=LEAGUE_DATA)
        )
        capture_league_data(SEASON, connector=connector, data_root=tmp_path)
        second = capture_league_data(SEASON, connector=connector, data_root=tmp_path)
        assert second.written is False


class TestCaptureMatchData:
    def _seed_league_data(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from fpl.storage.raw_io import RawArtifact

        artifact = RawArtifact(
            source="understat",
            endpoint="league_data",
            season=SEASON,
            url=f"{BASE}/getLeagueData/{LEAGUE}/{SEASON.start_year}",
            http_status=200,
            body=json.dumps(LEAGUE_DATA).encode(),
            fetched_at=datetime(2026, 8, 4, tzinfo=UTC),
            connector_version="1",
            content_type="json",
        )
        write_raw(artifact, data_root=tmp_path)

    @respx.mock
    def test_only_fetches_results_not_unplayed_fixtures(
        self, connector: UnderstatConnector, tmp_path: Path
    ) -> None:
        self._seed_league_data(tmp_path)
        for match_id in (1, 2, 3):
            respx.get(f"{BASE}/getMatchData/{match_id}").mock(
                return_value=httpx.Response(200, json=match_payload(match_id))
            )
        # Fixture 4 (isResult False) must never be requested.
        outcome = capture_match_data(SEASON, connector=connector, data_root=tmp_path)
        assert outcome.matches == 3
        assert outcome.chunks_written == 1

    @respx.mock
    def test_no_league_data_means_nothing_to_do(
        self, connector: UnderstatConnector, tmp_path: Path
    ) -> None:
        outcome = capture_match_data(SEASON, connector=connector, data_root=tmp_path)
        assert outcome.nothing_to_do

    @respx.mock
    def test_writes_one_chunk_per_batch(
        self, connector: UnderstatConnector, tmp_path: Path
    ) -> None:
        many_matches = {
            "teams": {},
            "players": [],
            "dates": [
                {"id": str(m), "isResult": True, "h": {"title": "A"}, "a": {"title": "B"}}
                for m in range(1, CHUNK_SIZE + 6)
            ],
        }
        artifact_body = json.dumps(many_matches).encode()
        from datetime import UTC, datetime

        from fpl.storage.raw_io import RawArtifact

        write_raw(
            RawArtifact(
                source="understat",
                endpoint="league_data",
                season=SEASON,
                url="x",
                http_status=200,
                body=artifact_body,
                fetched_at=datetime(2026, 8, 4, tzinfo=UTC),
                connector_version="1",
                content_type="json",
            ),
            data_root=tmp_path,
        )
        for match_id in range(1, CHUNK_SIZE + 6):
            respx.get(f"{BASE}/getMatchData/{match_id}").mock(
                return_value=httpx.Response(200, json=match_payload(match_id))
            )
        outcome = capture_match_data(SEASON, connector=connector, data_root=tmp_path)
        assert outcome.chunks_written == 2

    @respx.mock
    def test_resumes_without_refetching_completed_chunks(
        self, connector: UnderstatConnector, tmp_path: Path
    ) -> None:
        self._seed_league_data(tmp_path)
        calls = {1: 0, 2: 0, 3: 0}
        for match_id in (1, 2, 3):

            def _responder(request, match_id=match_id):
                calls[match_id] += 1
                return httpx.Response(200, json=match_payload(match_id))

            respx.get(f"{BASE}/getMatchData/{match_id}").mock(side_effect=_responder)

        capture_match_data(SEASON, connector=connector, data_root=tmp_path)
        capture_match_data(SEASON, connector=connector, data_root=tmp_path)
        assert all(count == 1 for count in calls.values())

    @respx.mock
    def test_chunk_holds_one_ndjson_record_per_match(
        self, connector: UnderstatConnector, tmp_path: Path
    ) -> None:
        self._seed_league_data(tmp_path)
        for match_id in (1, 2, 3):
            respx.get(f"{BASE}/getMatchData/{match_id}").mock(
                return_value=httpx.Response(200, json=match_payload(match_id))
            )
        capture_match_data(SEASON, connector=connector, data_root=tmp_path)
        chunks = list(paths.iter_chunks("understat", "match_data", SEASON, data_root=tmp_path))
        assert len(chunks) == 1
        body, meta = read_raw(chunks[0][1])
        records = [json.loads(line) for line in body.splitlines()]
        assert [r["match_id"] for r in records] == [1, 2, 3]
        assert meta["match_count"] == 3
