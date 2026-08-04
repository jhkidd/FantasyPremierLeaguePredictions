from __future__ import annotations

from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.features.team_context import TEAM_CONTEXT_COLUMNS, build_team_context_features
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet

SEASON = Season(2025)


def _write_team_fixture_facts(data_root: Path, rows: list[dict]) -> None:
    frame = pl.DataFrame(rows)
    out_dir = paths.facts_table("team_fixture", SEASON, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet")


def _team_fixture_row(**overrides: object) -> dict:
    row = {
        "season": str(SEASON),
        "fixture_id": 501,
        "team_id": 3,
        "opponent_team_id": 7,
        "was_home": True,
        "elo_rating": 1500.0,
        "opponent_elo_rating": 1450.0,
        "fixture_count_prior_7_days": 1,
        "fixture_count_prior_14_days": 2,
        "fixture_count_prior_28_days": 4,
        "odds_implied_win_prob": 0.5,
        "odds_implied_draw_prob": 0.3,
        "odds_implied_loss_prob": 0.2,
    }
    row.update(overrides)
    return row


class TestBuildTeamContextFeatures:
    def test_matching_row_is_joined_unmodified(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_team_fixture_facts(data_root, [_team_fixture_row()])

        result = build_team_context_features(
            SEASON, {(1, 501): 3}, data_root=data_root
        )

        features = result[(1, 501)]
        assert features["elo_rating"] == 1500.0
        assert features["opponent_elo_rating"] == 1450.0
        assert features["odds_implied_win_prob"] == 0.5
        assert features["fixture_count_prior_7_days"] == 1

    def test_unresolved_team_yields_all_nulls(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_team_fixture_facts(data_root, [_team_fixture_row()])

        result = build_team_context_features(
            SEASON, {(1, 501): None}, data_root=data_root
        )

        features = result[(1, 501)]
        assert all(features[column] is None for column in TEAM_CONTEXT_COLUMNS)

    def test_no_matching_team_fixture_row_yields_all_nulls(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_team_fixture_facts(data_root, [_team_fixture_row(team_id=99)])

        result = build_team_context_features(
            SEASON, {(1, 501): 3}, data_root=data_root
        )

        features = result[(1, 501)]
        assert all(features[column] is None for column in TEAM_CONTEXT_COLUMNS)

    def test_no_team_fixture_facts_table_yields_all_nulls(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"

        result = build_team_context_features(
            SEASON, {(1, 501): 3}, data_root=data_root
        )

        features = result[(1, 501)]
        assert all(features[column] is None for column in TEAM_CONTEXT_COLUMNS)

    def test_multiple_fixtures_for_same_team_resolve_independently(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_team_fixture_facts(
            data_root,
            [
                _team_fixture_row(fixture_id=501, elo_rating=1500.0),
                _team_fixture_row(fixture_id=502, elo_rating=1510.0),
            ],
        )

        result = build_team_context_features(
            SEASON, {(1, 501): 3, (1, 502): 3}, data_root=data_root
        )

        assert result[(1, 501)]["elo_rating"] == 1500.0
        assert result[(1, 502)]["elo_rating"] == 1510.0
