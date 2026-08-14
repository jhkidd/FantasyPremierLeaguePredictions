from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.features.team_resolution import TeamResolutionDiagnostics, resolve_teams
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet

SEASON = Season(2025)


def _write_players(data_root: Path, rows: list[dict]) -> None:
    frame = pl.DataFrame(rows)
    out_dir = paths.staged_table("players", SEASON, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet")


def _write_player_fixture_facts(data_root: Path, rows: list[dict]) -> None:
    frame = pl.DataFrame(rows)
    out_dir = paths.facts_table("player_fixture", SEASON, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet")


def _target_fixtures(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows)


class TestResolveTeams:
    def test_future_target_fixture_uses_current_players_table_team(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root,
            [{"player_id": 1, "team_id": 3, "element_type": 3, "now_cost": 75}],
        )
        target = _target_fixtures(
            [{"fixture_id": 501, "kickoff_time": datetime(2026, 8, 20, tzinfo=UTC)}]
        )
        as_of = datetime(2026, 8, 15, tzinfo=UTC)

        result, diagnostics = resolve_teams(SEASON, [1], target, as_of=as_of, data_root=data_root)

        assert result[(1, 501)] == 3
        assert diagnostics.fallback_to_current_team == (1,)

    def test_exact_played_fixture_uses_its_own_recorded_team_id(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root,
            [{"player_id": 1, "team_id": 99, "element_type": 3, "now_cost": 75}],
        )
        _write_player_fixture_facts(
            data_root,
            [
                {
                    "fixture_id": 501,
                    "player_id": 1,
                    "team_id": 3,
                    "kickoff_time": datetime(2025, 8, 16, tzinfo=UTC),
                }
            ],
        )
        target = _target_fixtures(
            [{"fixture_id": 501, "kickoff_time": datetime(2025, 8, 16, tzinfo=UTC)}]
        )
        as_of = datetime(2025, 8, 15, tzinfo=UTC)

        result, diagnostics = resolve_teams(SEASON, [1], target, as_of=as_of, data_root=data_root)

        assert result[(1, 501)] == 3
        assert diagnostics.fallback_to_current_team == ()

    def test_no_snapshot_falls_back_to_most_recent_prior_fixture_team(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root,
            [{"player_id": 1, "team_id": 99, "element_type": 3, "now_cost": 75}],
        )
        _write_player_fixture_facts(
            data_root,
            [
                {
                    "fixture_id": 400,
                    "player_id": 1,
                    "team_id": 3,
                    "kickoff_time": datetime(2025, 8, 9, tzinfo=UTC),
                }
            ],
        )
        # Target fixture 501 has not been played by this player yet (a
        # not-yet-reconciled future gameweek), so resolution must fall back
        # to fixture 400's recorded team, never same-day-or-later data.
        target = _target_fixtures(
            [{"fixture_id": 501, "kickoff_time": datetime(2025, 8, 23, tzinfo=UTC)}]
        )
        as_of = datetime(2025, 8, 16, tzinfo=UTC)

        result, diagnostics = resolve_teams(SEASON, [1], target, as_of=as_of, data_root=data_root)

        assert result[(1, 501)] == 3
        assert diagnostics.fallback_to_current_team == ()

    def test_no_history_at_all_falls_back_to_current_team_and_is_flagged(
        self, tmp_path: Path
    ) -> None:
        data_root = tmp_path / "data"
        _write_players(
            data_root,
            [{"player_id": 42, "team_id": 7, "element_type": 4, "now_cost": 45}],
        )
        target = _target_fixtures(
            [{"fixture_id": 501, "kickoff_time": datetime(2025, 8, 16, tzinfo=UTC)}]
        )
        as_of = datetime(2025, 8, 10, tzinfo=UTC)

        result, diagnostics = resolve_teams(SEASON, [42], target, as_of=as_of, data_root=data_root)

        assert result[(42, 501)] == 7
        assert diagnostics.fallback_to_current_team == (42,)
        assert diagnostics.fallback_count == 1

    def test_diagnostics_default_is_empty(self) -> None:
        diagnostics = TeamResolutionDiagnostics()
        assert diagnostics.fallback_count == 0
