from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from fpl.config import Season
from fpl.staging.fpl_api import stage_bootstrap_static, stage_event_live, stage_fixtures

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "fpl"
SEASON = Season(2026)


@pytest.fixture
def bootstrap_body() -> bytes:
    return (FIXTURES_DIR / "bootstrap_static.json").read_bytes()


@pytest.fixture
def fixtures_body() -> bytes:
    return (FIXTURES_DIR / "fixtures.json").read_bytes()


class TestStageBootstrapStatic:
    def test_stages_all_three_tables(self, bootstrap_body: bytes):
        staged = stage_bootstrap_static(bootstrap_body, SEASON)
        assert staged.players.height > 0
        assert staged.teams.height > 0
        assert staged.events.height > 0

    def test_season_column_present_and_first(self, bootstrap_body: bytes):
        staged = stage_bootstrap_static(bootstrap_body, SEASON)
        assert staged.players.columns[0] == "season"
        assert staged.players["season"].unique().to_list() == ["2026-27"]

    def test_defensive_columns_present(self, bootstrap_body: bytes):
        staged = stage_bootstrap_static(bootstrap_body, SEASON)
        defensive_cols = (
            "clearances_blocks_interceptions",
            "tackles",
            "recoveries",
            "defensive_contribution",
        )
        for col in defensive_cols:
            assert col in staged.players.columns

    def test_no_unknown_columns_go_unreported(self, bootstrap_body: bytes):
        staged = stage_bootstrap_static(bootstrap_body, SEASON)
        players_report = staged.reports[0]
        # ep_next/form etc are on the drop-list, so they must never surface as unknown.
        assert "ep_next" not in players_report.unknown_columns
        assert "form" not in players_report.unknown_columns

    def test_player_key_is_unique(self, bootstrap_body: bytes):
        staged = stage_bootstrap_static(bootstrap_body, SEASON)
        assert staged.players.select(["season", "player_id"]).is_duplicated().sum() == 0

    def test_events_carry_data_checked(self, bootstrap_body: bytes):
        """The finalisation signal ``event_live`` ingestion gates on
        (close-live-ingestion-gap.md) — already published on every event,
        so no dedicated endpoint is needed for it."""
        staged = stage_bootstrap_static(bootstrap_body, SEASON)
        assert "data_checked" in staged.events.columns
        assert staged.events["data_checked"].dtype == pl.Boolean


class TestStageFixtures:
    def test_stages_fixtures(self, fixtures_body: bytes):
        staged, report = stage_fixtures(fixtures_body, SEASON)
        assert staged.height > 0
        assert report.rows_out == staged.height

    def test_team_and_score_columns_present(self, fixtures_body: bytes):
        staged, _ = stage_fixtures(fixtures_body, SEASON)
        for col in ("team_h", "team_a", "team_h_score", "team_a_score", "finished"):
            assert col in staged.columns

    def test_stats_column_dropped(self, fixtures_body: bytes):
        staged, report = stage_fixtures(fixtures_body, SEASON)
        assert "stats" not in staged.columns
        assert "stats" not in report.unknown_columns

    def test_fixture_key_is_unique(self, fixtures_body: bytes):
        staged, _ = stage_fixtures(fixtures_body, SEASON)
        assert staged.select(["season", "fixture_id"]).is_duplicated().sum() == 0


class TestStageEventLive:
    """``event_live`` is the current, in-progress season's only source of
    ``player_fixture_stats`` (close-live-ingestion-gap.md). ``players`` and
    ``fixtures`` here are already-staged tables, not raw payloads."""

    @pytest.fixture
    def players(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "player_id": [10, 20, 30],
                "code": [1001, 1002, 1003],
                "team_id": [1, 2, 1],
                "element_type": [1, 4, 2],
            }
        )

    @pytest.fixture
    def fixtures(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "fixture_id": [100],
                "team_h": [1],
                "team_a": [2],
                "kickoff_time": ["2026-08-21T19:00:00Z"],
            }
        )

    @staticmethod
    def _element(player_id: int, fixture_ids: list[int], **stats) -> dict:
        base = {
            "minutes": 0,
            "goals_scored": 0,
            "assists": 0,
            "clean_sheets": 0,
            "goals_conceded": 0,
            "own_goals": 0,
            "penalties_saved": 0,
            "penalties_missed": 0,
            "yellow_cards": 0,
            "red_cards": 0,
            "saves": 0,
            "bonus": 0,
            "bps": 0,
            "clearances_blocks_interceptions": 0,
            "tackles": 0,
            "recoveries": 0,
            "defensive_contribution": 0,
            "starts": 0,
            "expected_goals": "0.00",
            "expected_assists": "0.00",
            "expected_goal_involvements": "0.00",
            "expected_goals_conceded": "0.00",
            "total_points": 0,
            "in_dreamteam": False,
            "played": bool(fixture_ids),
        }
        base.update(stats)
        return {
            "id": player_id,
            "stats": base,
            "explain": [{"fixture": f, "stats": []} for f in fixture_ids],
            "modified": False,
        }

    def _body(self, elements: list[dict]) -> bytes:
        return json.dumps({"elements": elements}).encode()

    def test_stages_one_row_per_played_fixture(self, players, fixtures):
        body = self._body(
            [
                self._element(10, [100], minutes=90, saves=3, total_points=7),
                self._element(20, [100], minutes=75, goals_scored=1, total_points=9),
            ]
        )
        staged, report = stage_event_live(body, SEASON, 1, players=players, fixtures=fixtures)
        assert staged.height == 2
        assert report.rows_out == 2

    def test_derives_was_home_and_opponent_from_the_players_own_team(self, players, fixtures):
        body = self._body(
            [
                self._element(10, [100], minutes=90),  # team 1 == team_h
                self._element(20, [100], minutes=75),  # team 2 == team_a
            ]
        )
        staged, _ = stage_event_live(body, SEASON, 1, players=players, fixtures=fixtures)
        by_player = {row["player_id"]: row for row in staged.iter_rows(named=True)}
        assert by_player[10]["was_home"] is True
        assert by_player[10]["opponent_team"] == 2
        assert by_player[20]["was_home"] is False
        assert by_player[20]["opponent_team"] == 1

    def test_derives_position_from_element_type(self, players, fixtures):
        body = self._body([self._element(10, [100], minutes=90)])
        staged, _ = stage_event_live(body, SEASON, 1, players=players, fixtures=fixtures)
        assert staged["position"].to_list() == ["GK"]

    def test_bps_input_columns_are_null(self, players, fixtures):
        """FPL retired this Opta-era breakdown before 2020/21 (Finding 2);
        the live season must match every other recent season, not invent
        new missingness."""
        body = self._body([self._element(10, [100], minutes=90)])
        staged, _ = stage_event_live(body, SEASON, 1, players=players, fixtures=fixtures)
        for column in ("attempted_passes", "key_passes", "big_chances_created", "winning_goals"):
            assert staged[column].to_list() == [None]

    def test_defensive_and_expected_columns_are_populated(self, players, fixtures):
        body = self._body(
            [
                self._element(
                    10,
                    [100],
                    minutes=90,
                    clearances_blocks_interceptions=4,
                    tackles=2,
                    recoveries=6,
                    defensive_contribution=12,
                    expected_goals_conceded="1.31",
                )
            ]
        )
        staged, _ = stage_event_live(body, SEASON, 1, players=players, fixtures=fixtures)
        row = staged.row(0, named=True)
        assert row["clearances_blocks_interceptions"] == 4
        assert row["tackles"] == 2
        assert row["recoveries"] == 6
        assert row["defensive_contribution"] == 12
        assert row["expected_goals_conceded"] == pytest.approx(1.31)

    def test_a_player_absent_from_the_players_table_gets_null_derived_columns(self, fixtures):
        empty_players = pl.DataFrame(
            {"player_id": [], "code": [], "team_id": [], "element_type": []},
            schema={
                "player_id": pl.Int64,
                "code": pl.Int64,
                "team_id": pl.Int64,
                "element_type": pl.Int64,
            },
        )
        body = self._body([self._element(999, [100], minutes=90)])
        staged, _ = stage_event_live(body, SEASON, 1, players=empty_players, fixtures=fixtures)
        row = staged.row(0, named=True)
        assert row["was_home"] is None
        assert row["opponent_team"] is None
        assert row["position"] is None

    def test_a_blank_gameweek_player_produces_no_row(self, players, fixtures):
        body = self._body([{"id": 10, "stats": {"minutes": 0}, "explain": [], "modified": False}])
        staged, report = stage_event_live(body, SEASON, 1, players=players, fixtures=fixtures)
        assert staged.height == 0
        assert report.excluded["double_gameweek"] == 0

    def test_a_double_gameweek_player_is_skipped_and_counted(self, players, fixtures):
        body = self._body([self._element(10, [100, 101], minutes=180)])
        staged, report = stage_event_live(body, SEASON, 1, players=players, fixtures=fixtures)
        assert staged.height == 0
        assert report.excluded["double_gameweek"] == 1

    def test_season_column_present_and_first(self, players, fixtures):
        body = self._body([self._element(10, [100], minutes=90)])
        staged, _ = stage_event_live(body, SEASON, 1, players=players, fixtures=fixtures)
        assert staged.columns[0] == "season"
        assert staged["season"].unique().to_list() == ["2026-27"]

    def test_key_is_unique(self, players, fixtures):
        body = self._body(
            [
                self._element(10, [100], minutes=90),
                self._element(20, [100], minutes=75),
            ]
        )
        staged, _ = stage_event_live(body, SEASON, 1, players=players, fixtures=fixtures)
        assert staged.select(["player_id", "fixture_id"]).is_duplicated().sum() == 0

    def test_no_unknown_columns_go_unreported(self, players, fixtures):
        """clean_sheets/influence/etc are declared drops; a real `event_live`
        payload carries them on every element."""
        body = self._body([self._element(10, [100], minutes=90)])
        _, report = stage_event_live(body, SEASON, 1, players=players, fixtures=fixtures)
        assert not report.unknown_columns


class TestStageEventLiveTeamResolution:
    """A player's ``team`` in bootstrap-static only ever reflects their
    *current* club, so it misattributes a past gameweek's fixture once
    they've since transferred (close-live-ingestion-gap.md). These cases
    cover the 3-source validated waterfall added to resolve that."""

    # Fixture 200 is team 2 (home) vs team 3 (away); every case below has a
    # player whose *current* club (in ``players``) is team 1 — a club not
    # even playing this fixture — so a correct result can only come from
    # ``match_side_team`` or ``team_by_player``, never from source 3 alone.
    @pytest.fixture
    def players(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "player_id": [10, 20, 30, 40],
                "code": [1001, 1002, 1003, 1004],
                "team_id": [1, 1, 2, 3],
                "element_type": [1, 1, 1, 1],
            }
        )

    @pytest.fixture
    def fixtures(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "fixture_id": [200],
                "team_h": [2],
                "team_a": [3],
                "kickoff_time": ["2026-08-21T19:00:00Z"],
            }
        )

    @staticmethod
    def _element(player_id: int, minutes: int = 90) -> dict:
        return TestStageEventLive._element(player_id, [200], minutes=minutes)

    def _body(self, elements: list[dict]) -> bytes:
        return json.dumps({"elements": elements}).encode()

    def test_match_side_team_wins_over_a_stale_current_team(self, players, fixtures):
        """Player 10's ``players.team_id`` (1) isn't in this fixture at all —
        a transfer since the last bootstrap-static capture. The fixture's own
        per-match stat breakdown says they played for team 2 (home)."""
        body = self._body([self._element(10)])
        match_side_team = pl.DataFrame({"fixture_id": [200], "player_id": [10], "team_id": [2]})
        staged, _ = stage_event_live(
            body, SEASON, 1, players=players, fixtures=fixtures, match_side_team=match_side_team
        )
        row = staged.row(0, named=True)
        assert row["was_home"] is True
        assert row["opponent_team"] == 3

    def test_historical_snapshot_resolves_a_zero_involvement_substitute(self, players, fixtures):
        """Player 20 has zero minutes, so they never appear in the fixture's
        own per-match stat breakdown — only a historical bootstrap-static
        snapshot (as of this gameweek's deadline) can place them."""
        body = self._body([self._element(20, minutes=0)])
        team_by_player = pl.DataFrame({"player_id": [20], "team_id": [3]})
        staged, _ = stage_event_live(
            body, SEASON, 1, players=players, fixtures=fixtures, team_by_player=team_by_player
        )
        row = staged.row(0, named=True)
        assert row["was_home"] is False
        assert row["opponent_team"] == 2

    def test_current_team_is_the_last_resort_for_a_brand_new_signing(self, players, fixtures):
        """Player 30 is a mid-season signing absent from every historical
        snapshot (they hadn't joined the league yet) and from this fixture's
        stat breakdown (an unused substitute) — only their current team
        (2, which matches team_h here) can resolve them."""
        body = self._body([self._element(30, minutes=0)])
        staged, _ = stage_event_live(body, SEASON, 1, players=players, fixtures=fixtures)
        row = staged.row(0, named=True)
        assert row["was_home"] is True
        assert row["opponent_team"] == 3

    def test_a_stale_snapshot_does_not_shadow_a_valid_current_team(self, players, fixtures):
        """Player 40's historical snapshot (team 99) is itself wrong/lagging
        and matches neither side of the fixture — it must not block the
        fallback to their current team (3, team_a here), which does."""
        team_by_player = pl.DataFrame({"player_id": [40], "team_id": [99]})
        body = self._body([self._element(40, minutes=0)])
        staged, _ = stage_event_live(
            body, SEASON, 1, players=players, fixtures=fixtures, team_by_player=team_by_player
        )
        row = staged.row(0, named=True)
        assert row["was_home"] is False
        assert row["opponent_team"] == 2

    def test_an_unresolvable_player_gets_null_rather_than_a_wrong_guess(self, players, fixtures):
        """Player 20's current team (1) and a snapshot both point at a club
        that isn't even in this fixture — with no ``match_side_team`` entry
        either (zero involvement), null is the honest outcome, not a guess."""
        team_by_player = pl.DataFrame({"player_id": [20], "team_id": [99]})
        body = self._body([self._element(20, minutes=0)])
        staged, _ = stage_event_live(
            body, SEASON, 1, players=players, fixtures=fixtures, team_by_player=team_by_player
        )
        row = staged.row(0, named=True)
        assert row["was_home"] is None
        assert row["opponent_team"] is None
