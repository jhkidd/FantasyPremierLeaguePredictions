"""Tests for :mod:`fpl.inference.naive` — trailing-mean predictions for the
naive-only components at inference time (Phase C step 5,
`.github/context/subsystem3-close-and-mvp-site.md`)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.inference.naive import NAIVE_ONLY_COMPONENTS, predict_naive_components
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet
from fpl.training.evaluation import NAIVE_ONLY_COMPONENTS as EVALUATION_NAIVE_ONLY_COMPONENTS

SEASON = Season(2025)

_FACTS_COLUMNS = [
    "season",
    "fixture_id",
    "player_id",
    "team_id",
    "kickoff_time",
    "event",
    "minutes",
    "saves",
    "yellow_cards",
    "red_cards",
    "penalties_saved",
    "penalties_missed",
    "own_goals",
]


def _facts_row(**overrides: object) -> dict:
    row: dict = dict.fromkeys(_FACTS_COLUMNS, 0)
    row.update({"season": str(SEASON)})
    row.update(overrides)
    return row


def _write_facts(data_root: Path, season: Season, rows: list[dict]) -> None:
    frame = pl.DataFrame(rows)
    out_dir = paths.facts_table("player_fixture", season, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet")


class TestNaiveOnlyComponentsSharedWithEvaluation:
    def test_matches_evaluation_module_constant(self) -> None:
        assert NAIVE_ONLY_COMPONENTS == EVALUATION_NAIVE_ONLY_COMPONENTS


class TestPredictNaiveComponents:
    def test_no_facts_table_yields_null_for_every_player(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"

        result = predict_naive_components(
            SEASON, datetime(2025, 8, 20, tzinfo=UTC), [1, 2], data_root=data_root
        )

        assert result["player_id"].to_list() == [1, 2]
        for component in NAIVE_ONLY_COMPONENTS:
            assert result[f"naive_{component}"].to_list() == [None, None]

    def test_empty_player_ids_returns_empty_frame(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"

        result = predict_naive_components(
            SEASON, datetime(2025, 8, 20, tzinfo=UTC), [], data_root=data_root
        )

        assert result.height == 0
        assert "naive_saves" in result.columns

    def test_no_prior_history_yields_null(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=501,
                    player_id=1,
                    event=1,
                    kickoff_time=datetime(2025, 8, 23, 14, tzinfo=UTC),
                    saves=3,
                )
            ],
        )

        # as_of is before this player's only fixture - no history exists yet.
        result = predict_naive_components(
            SEASON, datetime(2025, 8, 10, tzinfo=UTC), [1], data_root=data_root
        )

        assert result["naive_saves"].to_list() == [None]

    def test_mean_of_prior_fixtures_before_as_of(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=400,
                    player_id=1,
                    event=1,
                    kickoff_time=datetime(2025, 8, 16, 14, tzinfo=UTC),
                    saves=2,
                    yellow_cards=1,
                ),
                _facts_row(
                    fixture_id=450,
                    player_id=1,
                    event=2,
                    kickoff_time=datetime(2025, 8, 23, 14, tzinfo=UTC),
                    saves=4,
                    yellow_cards=0,
                ),
                # Not yet played (kickoff_time after as_of) - excluded.
                _facts_row(
                    fixture_id=600,
                    player_id=1,
                    event=3,
                    kickoff_time=datetime(2025, 9, 6, 14, tzinfo=UTC),
                    saves=100,
                ),
            ],
        )

        result = predict_naive_components(
            SEASON, datetime(2025, 8, 30, tzinfo=UTC), [1], data_root=data_root
        )

        assert result["naive_saves"].to_list() == [3.0]
        assert result["naive_yellow_cards"].to_list() == [0.5]

    def test_window_limits_history_to_most_recent_fixtures(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        rows = [
            _facts_row(
                fixture_id=100 + i,
                player_id=1,
                event=i + 1,
                kickoff_time=datetime(2025, 8, 16, tzinfo=UTC).replace(day=16 + i),
                saves=i,
            )
            for i in range(6)
        ]
        _write_facts(data_root, SEASON, rows)

        result = predict_naive_components(
            SEASON,
            datetime(2025, 9, 1, tzinfo=UTC),
            [1],
            window=3,
            data_root=data_root,
        )

        # Fixtures had saves=[0,1,2,3,4,5]; only the most recent 3 (3,4,5) count.
        assert result["naive_saves"].to_list() == [4.0]

    def test_different_players_computed_independently(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_facts(
            data_root,
            SEASON,
            [
                _facts_row(
                    fixture_id=400,
                    player_id=1,
                    event=1,
                    kickoff_time=datetime(2025, 8, 16, 14, tzinfo=UTC),
                    saves=10,
                ),
                _facts_row(
                    fixture_id=401,
                    player_id=2,
                    event=1,
                    kickoff_time=datetime(2025, 8, 16, 14, tzinfo=UTC),
                    saves=0,
                ),
            ],
        )

        result = predict_naive_components(
            SEASON, datetime(2025, 8, 30, tzinfo=UTC), [1, 2], data_root=data_root
        )

        by_player = dict(
            zip(result["player_id"].to_list(), result["naive_saves"].to_list(), strict=True)
        )
        assert by_player == {1: 10.0, 2: 0.0}
