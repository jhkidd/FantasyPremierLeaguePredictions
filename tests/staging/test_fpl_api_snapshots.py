from __future__ import annotations

import json
from datetime import UTC, datetime

from fpl.config import Season
from fpl.staging.fpl_api import (
    stage_availability_snapshots,
    stage_entry_snapshots,
    stage_manager_picks,
    stage_price_snapshots,
)

SEASON = Season(2026)
T0 = datetime(2026, 8, 1, tzinfo=UTC)
T1 = datetime(2026, 8, 2, tzinfo=UTC)


def _bootstrap_body(*, now_cost: int, status: str, news: str = "") -> bytes:
    payload = {
        "elements": [
            {
                "id": 1,
                "now_cost": now_cost,
                "cost_change_event": 0,
                "selected_by_percent": "12.3",
                "transfers_in_event": 100,
                "transfers_out_event": 50,
                "status": status,
                "news": news,
                "chance_of_playing_next_round": 100 if status == "a" else 0,
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


class TestStagePriceSnapshots:
    def test_two_captures_stack_without_duplicating(self):
        captures = [
            (_bootstrap_body(now_cost=50, status="a"), T0),
            (_bootstrap_body(now_cost=51, status="a"), T1),
        ]
        staged, reports = stage_price_snapshots(captures, SEASON)
        assert staged.height == 2
        assert len(reports) == 2
        assert sorted(staged["now_cost"].to_list()) == [50, 51]

    def test_as_of_ts_recovered_per_capture(self):
        captures = [(_bootstrap_body(now_cost=50, status="a"), T0)]
        staged, _ = stage_price_snapshots(captures, SEASON)
        assert staged["as_of_ts"].to_list() == [T0.isoformat()]

    def test_empty_captures_yield_empty_frame(self):
        staged, reports = stage_price_snapshots([], SEASON)
        assert staged.height == 0
        assert reports == []


class TestStageAvailabilitySnapshots:
    def test_status_and_news_captured_over_time(self):
        captures = [
            (_bootstrap_body(now_cost=50, status="a"), T0),
            (_bootstrap_body(now_cost=50, status="i", news="Hamstring injury"), T1),
        ]
        staged, _ = stage_availability_snapshots(captures, SEASON)
        assert staged.sort("as_of_ts")["status"].to_list() == ["a", "i"]
        assert staged.sort("as_of_ts")["news"].to_list() == ["", "Hamstring injury"]


class TestStageEntrySnapshots:
    def test_stages_one_row_per_capture(self):
        payload = {
            "id": 2282251,
            "summary_overall_points": 55,
            "summary_overall_rank": 1_000_000,
            "summary_event_points": 55,
            "last_deadline_bank": 5,
            "last_deadline_value": 1000,
            "last_deadline_total_transfers": 0,
        }
        body = json.dumps(payload).encode("utf-8")
        staged, reports = stage_entry_snapshots([(body, T0)], SEASON)
        assert staged.height == 1
        assert staged["entry_id"].to_list() == [2282251]
        assert len(reports) == 1


class TestStageManagerPicks:
    def test_flattens_picks_and_carries_contaminated_flag(self):
        records = [
            {
                "entry": 111,
                "event": 3,
                "fetched_at": T0.isoformat(),
                "contaminated": True,
                "payload": {
                    "picks": [
                        {
                            "element": 10,
                            "position": 1,
                            "multiplier": 2,
                            "is_captain": True,
                            "is_vice_captain": False,
                        },
                        {
                            "element": 11,
                            "position": 2,
                            "multiplier": 1,
                            "is_captain": False,
                            "is_vice_captain": True,
                        },
                    ]
                },
            }
        ]
        staged, report = stage_manager_picks(records, SEASON, "mini")
        assert staged.height == 2
        assert set(staged["contaminated"].to_list()) == {True}
        assert staged["cohort"].unique().to_list() == ["mini"]
        assert report.rows_out == 2

    def test_two_cohorts_are_never_pooled(self):
        record = {
            "entry": 1,
            "event": 1,
            "contaminated": False,
            "payload": {"picks": [{"element": 1, "position": 1, "multiplier": 1}]},
        }
        mini, _ = stage_manager_picks([record], SEASON, "mini")
        elite, _ = stage_manager_picks([record], SEASON, "elite")
        assert mini["cohort"].to_list() == ["mini"]
        assert elite["cohort"].to_list() == ["elite"]
