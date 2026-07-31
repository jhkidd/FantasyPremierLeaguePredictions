from __future__ import annotations

from pathlib import Path

import pytest

from fpl.config import Season
from fpl.staging.fpl_api import stage_bootstrap_static, stage_fixtures

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
