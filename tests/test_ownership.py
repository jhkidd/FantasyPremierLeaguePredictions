from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from fpl.config import Season, SourceConfig
from fpl.ownership import (
    CHUNK_SIZE,
    ELITE_COHORT,
    ELITE_FIRST_EVENT,
    MINI_COHORT,
    SELF_COHORT,
    CaptureTarget,
    League,
    capture_ownership,
    collect_entry_ids,
    discover_private_leagues,
    elite_target,
    entries_per_page,
    load_latest_bootstrap,
    mini_target,
    resolve_capture_event,
    self_target,
)
from fpl.sources.errors import TransientError
from fpl.sources.fetcher import HttpFetcher
from fpl.sources.fpl_api import FplApiConnector
from fpl.storage import paths
from fpl.storage.raw_io import read_raw

BASE = "https://fpl.test/api"
SEASON = Season(2026)
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def event_row(event_id: int, deadline: str, *, finished: bool = False) -> dict:
    return {"id": event_id, "deadline_time": deadline, "finished": finished}


class TestResolveCaptureEvent:
    """The window is deadline passed and gameweek not finished — days, not the
    90 minutes to kickoff, because auto-subs are applied at the end (spec §6.1)."""

    def test_before_the_deadline_there_is_nothing_to_do(self) -> None:
        events = [event_row(1, "2026-08-21T17:30:00Z")]
        before = datetime(2026, 8, 21, 17, 0, tzinfo=UTC)
        assert resolve_capture_event({"events": events}, before, set()) is None

    def test_inside_the_window_returns_the_event(self) -> None:
        events = [event_row(1, "2026-08-21T17:30:00Z")]
        assert resolve_capture_event({"events": events}, NOW, set()) == 1

    def test_a_finished_gameweek_is_closed(self) -> None:
        events = [event_row(1, "2026-08-21T17:30:00Z", finished=True)]
        assert resolve_capture_event({"events": events}, NOW, set()) is None

    def test_an_already_captured_gameweek_is_skipped(self) -> None:
        events = [event_row(1, "2026-08-21T17:30:00Z")]
        assert resolve_capture_event({"events": events}, NOW, {1}) is None

    def test_two_open_events_take_the_earlier(self) -> None:
        """Around a double gameweek two can look open; the earlier expires first."""
        events = [
            event_row(1, "2026-08-21T17:30:00Z"),
            event_row(2, "2026-08-22T10:00:00Z"),
        ]
        assert resolve_capture_event({"events": events}, NOW, set()) == 1

    def test_the_earlier_event_being_captured_moves_on_to_the_next(self) -> None:
        events = [
            event_row(1, "2026-08-21T17:30:00Z"),
            event_row(2, "2026-08-22T10:00:00Z"),
        ]
        assert resolve_capture_event({"events": events}, NOW, {1}) == 2

    def test_first_event_floor_excludes_earlier_gameweeks(self) -> None:
        """The elite cohort cannot start at GW1: the overall league has no
        ranking until a gameweek has been scored."""
        events = [event_row(1, "2026-08-21T17:30:00Z")]
        payload = {"events": events}
        assert resolve_capture_event(payload, NOW, set(), first_event=ELITE_FIRST_EVENT) is None
        assert resolve_capture_event(payload, NOW, set(), first_event=1) == 1

    @pytest.mark.parametrize("payload", [{}, {"events": None}, {"events": []}])
    def test_missing_events_never_crash(self, payload: dict) -> None:
        assert resolve_capture_event(payload, NOW, set()) is None

    def test_malformed_rows_are_ignored_not_fatal(self) -> None:
        payload = {"events": ["nonsense", {"id": "x"}, {"deadline_time": 5}, event_row(3, "bad")]}
        assert resolve_capture_event(payload, NOW, set()) is None

    def test_naive_deadlines_are_treated_as_utc(self) -> None:
        events = [event_row(1, "2026-08-21T17:30:00")]
        assert resolve_capture_event({"events": events}, NOW, set()) == 1


class TestEntriesPerPage:
    def test_reads_rows_and_the_next_page_flag(self) -> None:
        payload = {"standings": {"results": [{"entry": 1}, {"entry": 2}], "has_next": True}}
        rows, has_next = entries_per_page(payload)
        assert [row["entry"] for row in rows] == [1, 2]
        assert has_next is True

    @pytest.mark.parametrize(
        "payload",
        [{}, {"standings": None}, {"standings": {}}, {"standings": {"results": None}}],
    )
    def test_malformed_pages_yield_nothing(self, payload: dict) -> None:
        assert entries_per_page(payload) == ([], False)

    def test_an_empty_league_is_a_valid_answer(self) -> None:
        """The overall league is empty until a gameweek is scored (verified live)."""
        payload = {"standings": {"results": [], "has_next": False}}
        assert entries_per_page(payload) == ([], False)


@pytest.fixture
def connector() -> FplApiConnector:
    fetcher = HttpFetcher(
        SourceConfig("test", min_request_interval=0.0, timeout=1.0, max_attempts=1),
        user_agent="test-agent",
        sleep=lambda _s: None,
    )
    return FplApiConnector(SEASON, fetcher=fetcher, base_url=BASE)


def mock_standings(league_id: int, pages: list[list[int]]) -> None:
    for index, entries in enumerate(pages, start=1):
        respx.get(
            f"{BASE}/leagues-classic/{league_id}/standings/",
            params={"page_standings": index},
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "league": {"id": league_id},
                    "standings": {
                        "results": [{"entry": e, "rank": e} for e in entries],
                        "has_next": index < len(pages),
                        "page": index,
                    },
                },
            )
        )


def mock_picks(entry_ids: list[int], event: int, *, auto_subs: set[int] | None = None) -> None:
    auto_subs = auto_subs or set()
    for entry_id in entry_ids:
        respx.get(f"{BASE}/entry/{entry_id}/event/{event}/picks/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "active_chip": None,
                    "automatic_subs": [{"element_in": 5, "element_out": 6}]
                    if entry_id in auto_subs
                    else [],
                    "entry_history": {"event": event, "points": 60},
                    "picks": [
                        {
                            "element": 100 + i,
                            "position": i + 1,
                            "multiplier": 1,
                            "is_captain": i == 0,
                            "is_vice_captain": i == 1,
                        }
                        for i in range(15)
                    ],
                },
            )
        )


class TestCollectEntryIds:
    @respx.mock
    def test_pages_until_exhausted(self, connector: FplApiConnector) -> None:
        mock_standings(999, [[1, 2, 3], [4, 5]])
        ids = collect_entry_ids(connector, mini_target(999, 1))
        assert ids == [1, 2, 3, 4, 5]

    @respx.mock
    def test_stops_once_top_is_reached(self, connector: FplApiConnector) -> None:
        """Paging past the cohort size would cost requests for rows we discard."""
        page_two = respx.get(
            f"{BASE}/leagues-classic/314/standings/", params={"page_standings": 2}
        ).mock(return_value=httpx.Response(200, json={"standings": {"results": [], "has_next": 0}}))
        mock_standings(314, [[1, 2, 3, 4, 5]])
        ids = collect_entry_ids(connector, elite_target(2, top=3))
        assert ids == [1, 2, 3]
        assert page_two.call_count == 0

    @respx.mock
    def test_an_empty_league_returns_nothing_rather_than_raising(
        self, connector: FplApiConnector
    ) -> None:
        mock_standings(314, [[]])
        assert collect_entry_ids(connector, elite_target(2, top=1000)) == []

    @respx.mock
    def test_stores_each_page_under_its_cohort(self, connector: FplApiConnector) -> None:
        mock_standings(999, [[1, 2]])
        collect_entry_ids(connector, mini_target(999, 3))
        stored = paths.latest_partition(
            "fpl", "league_standings", SEASON, cohort=MINI_COHORT, event=3
        )
        assert stored is not None


class TestCaptureOwnership:
    @respx.mock
    def test_writes_one_chunk_per_batch(self, connector: FplApiConnector) -> None:
        entries = list(range(1, CHUNK_SIZE + 6))
        mock_standings(314, [entries])
        mock_picks(entries, 2)
        outcome = capture_ownership(SEASON, elite_target(2, top=len(entries)), connector=connector)
        assert outcome.entries == len(entries)
        assert outcome.chunks_written == 2
        assert outcome.contaminated == 0

    @respx.mock
    def test_chunk_holds_one_ndjson_record_per_entry(self, connector: FplApiConnector) -> None:
        mock_standings(999, [[11, 12, 13]])
        mock_picks([11, 12, 13], 1)
        capture_ownership(SEASON, mini_target(999, 1), connector=connector)

        chunks = list(paths.iter_chunks("fpl", "entry_picks", SEASON, cohort=MINI_COHORT, event=1))
        assert len(chunks) == 1
        body, meta = read_raw(chunks[0][1])
        records = [json.loads(line) for line in body.splitlines()]
        assert [r["entry"] for r in records] == [11, 12, 13]
        assert records[0]["payload"]["picks"][0]["is_captain"] is True
        assert meta["cohort"] == MINI_COHORT
        assert meta["entry_count"] == 3

    @respx.mock
    def test_resumes_without_refetching_completed_chunks(self, connector: FplApiConnector) -> None:
        """Runners are ephemeral; a killed run must not start over."""
        entries = list(range(1, CHUNK_SIZE + 6))
        mock_standings(314, [entries])
        mock_picks(entries, 2)
        capture_ownership(SEASON, elite_target(2, top=len(entries)), connector=connector)

        second = capture_ownership(SEASON, elite_target(2, top=len(entries)), connector=connector)
        assert second.chunks_written == 0
        assert second.chunks_skipped == 2

    @respx.mock
    def test_a_partial_run_leaves_completed_chunks_intact(self, connector: FplApiConnector) -> None:
        entries = list(range(1, CHUNK_SIZE + 6))
        mock_standings(314, [entries])
        mock_picks(entries[:CHUNK_SIZE], 2)
        # The entries in the second chunk fail, so only the first should land.
        for entry_id in entries[CHUNK_SIZE:]:
            respx.get(f"{BASE}/entry/{entry_id}/event/2/picks/").mock(
                return_value=httpx.Response(500)
            )
        with pytest.raises(TransientError):
            capture_ownership(SEASON, elite_target(2, top=len(entries)), connector=connector)

        chunks = list(paths.iter_chunks("fpl", "entry_picks", SEASON, cohort=ELITE_COHORT, event=2))
        assert [index for index, _ in chunks] == [0]
        body, _ = read_raw(chunks[0][1])
        assert len(body.splitlines()) == CHUNK_SIZE

    @respx.mock
    def test_flags_contamination_without_discarding_the_record(
        self, connector: FplApiConnector
    ) -> None:
        """A non-empty automatic_subs means FPL rewrote the XI. The record is
        still worth keeping — auto-subs are reversible — but must be labelled."""
        mock_standings(999, [[1, 2, 3]])
        mock_picks([1, 2, 3], 1, auto_subs={2})
        outcome = capture_ownership(SEASON, mini_target(999, 1), connector=connector)
        assert outcome.contaminated == 1

        chunks = list(paths.iter_chunks("fpl", "entry_picks", SEASON, cohort=MINI_COHORT, event=1))
        body, meta = read_raw(chunks[0][1])
        records = [json.loads(line) for line in body.splitlines()]
        assert [r["contaminated"] for r in records] == [False, True, False]
        assert meta["contaminated_entries"] == 1

    @respx.mock
    def test_limit_caps_entries_for_rehearsal(self, connector: FplApiConnector) -> None:
        entries = list(range(1, 60))
        mock_standings(314, [entries])
        mock_picks(entries[:5], 2)
        outcome = capture_ownership(
            SEASON, elite_target(2, top=len(entries)), connector=connector, limit=5
        )
        assert outcome.entries == 5

    @respx.mock
    def test_an_empty_league_is_not_a_failure(self, connector: FplApiConnector) -> None:
        mock_standings(314, [[]])
        outcome = capture_ownership(SEASON, elite_target(2, top=1000), connector=connector)
        assert outcome.nothing_to_do
        assert outcome.chunks_written == 0

    @respx.mock
    def test_cohorts_are_stored_apart(self, connector: FplApiConnector) -> None:
        """Pooling an elite sample with a mini-league would describe nobody."""
        mock_standings(314, [[1, 2]])
        mock_standings(999, [[3, 4]])
        mock_picks([1, 2, 3, 4], 2)
        capture_ownership(SEASON, elite_target(2, top=2), connector=connector)
        capture_ownership(SEASON, mini_target(999, 2), connector=connector)

        for cohort, expected in ((ELITE_COHORT, [1, 2]), (MINI_COHORT, [3, 4])):
            chunks = list(paths.iter_chunks("fpl", "entry_picks", SEASON, cohort=cohort, event=2))
            body, _ = read_raw(chunks[0][1])
            assert [json.loads(line)["entry"] for line in body.splitlines()] == expected


class TestLoadLatestBootstrap:
    def test_returns_none_when_nothing_stored(self) -> None:
        assert load_latest_bootstrap(SEASON) is None

    def test_reads_back_what_ingestion_wrote(self, tmp_path: Path) -> None:
        from fpl.storage.raw_io import RawArtifact, write_raw

        artifact = RawArtifact(
            source="fpl",
            endpoint="bootstrap_static",
            season=SEASON,
            url="https://x",
            http_status=200,
            body=b'{"events": [{"id": 1}]}',
            fetched_at=datetime.now(UTC),
            connector_version="1",
        )
        write_raw(artifact, data_root=tmp_path)
        loaded = load_latest_bootstrap(SEASON, data_root=tmp_path)
        assert loaded == {"events": [{"id": 1}]}


class TestCaptureTarget:
    def test_elite_uses_the_overall_league(self) -> None:
        target = elite_target(5, top=1000)
        assert target.cohort == ELITE_COHORT
        assert target.league_id == 314
        assert target.top == 1000

    def test_mini_takes_every_member(self) -> None:
        target = mini_target(4321, 5)
        assert target == CaptureTarget(MINI_COHORT, 4321, 5, top=None)

    def test_self_names_one_entry_and_no_league(self) -> None:
        target = self_target(2282251, 5)
        assert target.cohort == SELF_COHORT
        assert target.league_id is None
        assert target.entry_ids == (2282251,)


class TestSelfCohort:
    """Our own squad expires with everyone else's, and without it there is no
    way to measure a recommendation against the decision actually taken."""

    def test_named_entries_need_no_standings_request(self, connector: FplApiConnector) -> None:
        """There is no league table for a cohort of one, so asking for one
        would 404 on a league ID that does not exist."""
        with respx.mock:
            assert collect_entry_ids(connector, self_target(2282251, 1)) == [2282251]

    @respx.mock
    def test_captures_our_own_picks(self, connector: FplApiConnector) -> None:
        mock_picks([2282251], 1)
        outcome = capture_ownership(SEASON, self_target(2282251, 1), connector=connector)
        assert outcome.entries == 1
        assert outcome.chunks_written == 1

        chunks = list(paths.iter_chunks("fpl", "entry_picks", SEASON, cohort=SELF_COHORT, event=1))
        body, _ = read_raw(chunks[0][1])
        assert [json.loads(line)["entry"] for line in body.splitlines()] == [2282251]

    def test_a_target_with_neither_league_nor_entries_is_a_programming_error(
        self, connector: FplApiConnector
    ) -> None:
        with pytest.raises(ValueError, match="neither a league nor"):
            collect_entry_ids(connector, CaptureTarget("odd", None, 1))


class TestDiscoverPrivateLeagues:
    """League IDs change every season and are unknown until someone creates the
    league, so hand-configuring one guarantees a silent gap."""

    def _mock_entry(self, entry_id: int, classic: list[dict]) -> None:
        respx.get(f"{BASE}/entry/{entry_id}/").mock(
            return_value=httpx.Response(
                200, json={"id": entry_id, "leagues": {"classic": classic, "h2h": []}}
            )
        )

    @respx.mock
    def test_ignores_the_leagues_everyone_is_put_into(
        self, connector: FplApiConnector, tmp_path: Path
    ) -> None:
        """Overall, Gameweek 1, country and sponsor leagues are marked 's' and
        are opponents in no meaningful sense."""
        self._mock_entry(
            2282251,
            [
                {"id": 314, "name": "Overall", "league_type": "s"},
                {"id": 261, "name": "England", "league_type": "s"},
                {"id": 431170, "name": "Adobe Express Badge League", "league_type": "s"},
                {"id": 555001, "name": "The Office", "league_type": "x"},
            ],
        )
        found = discover_private_leagues(connector, 2282251, data_root=tmp_path)
        assert found == [League(555001, "The Office")]

    @respx.mock
    def test_no_private_leagues_yet_is_not_an_error(
        self, connector: FplApiConnector, tmp_path: Path
    ) -> None:
        """The state of a freshly registered team, and of any team before the
        league admin has recreated the league for the new season."""
        self._mock_entry(2282251, [{"id": 314, "name": "Overall", "league_type": "s"}])
        assert discover_private_leagues(connector, 2282251, data_root=tmp_path) == []

    @respx.mock
    def test_reports_every_private_league_rather_than_choosing(
        self, connector: FplApiConnector, tmp_path: Path
    ) -> None:
        self._mock_entry(
            2282251,
            [
                {"id": 111, "name": "Office", "league_type": "x"},
                {"id": 222, "name": "Family", "league_type": "x"},
            ],
        )
        found = discover_private_leagues(connector, 2282251, data_root=tmp_path)
        assert [league.id for league in found] == [111, 222]

    @respx.mock
    def test_malformed_league_rows_are_skipped_not_fatal(
        self, connector: FplApiConnector, tmp_path: Path
    ) -> None:
        self._mock_entry(
            2282251,
            [
                "nonsense",
                {"id": "not-an-int", "name": "Bad", "league_type": "x"},
                {"id": 333, "league_type": "x"},
                {"id": 444, "name": "Good", "league_type": "x"},
            ],
        )
        found = discover_private_leagues(connector, 2282251, data_root=tmp_path)
        assert found == [League(444, "Good")]
