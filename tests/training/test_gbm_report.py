"""Tests for :mod:`fpl.training.gbm_report` (Phase B step 5)."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from fpl.training.gbm_report import render_gbm_report


def _render(tmp_path: Path) -> str:
    glm_component_metrics = pl.DataFrame(
        {
            "component": ["minutes", "goals_scored"],
            "position": ["MID", "MID"],
            "mae": [9.0, 0.15],
            "rmse": [14.0, 0.3],
            "poisson_deviance": [None, 0.5],
            "n": [50, 50],
        }
    )
    lgbm_component_metrics = pl.DataFrame(
        {
            "component": ["minutes", "goals_scored"],
            "position": ["MID", "MID"],
            "mae": [8.5, 0.13],
            "rmse": [13.0, 0.28],
            "poisson_deviance": [None, 0.45],
            "n": [50, 50],
        }
    )
    glm_points_report = pl.DataFrame(
        {
            "bucket": ["overall", "zeros"],
            "mae": [1.5, 0.5],
            "rmse": [2.0, 0.6],
            "poisson_deviance": [None, None],
            "n": [100, 20],
        }
    )
    lgbm_points_report = pl.DataFrame(
        {
            "bucket": ["overall", "zeros"],
            "mae": [1.3, 0.45],
            "rmse": [1.8, 0.55],
            "poisson_deviance": [None, None],
            "n": [100, 20],
        }
    )
    glm_gameweek_spearman = pl.DataFrame(
        {
            "season": ["2024-25", "2024-25"],
            "event": [1, 2],
            "n_players": [300, 300],
            "spearman": [0.6, 0.8],
        }
    )
    lgbm_gameweek_spearman = pl.DataFrame(
        {
            "season": ["2024-25", "2024-25"],
            "event": [1, 2],
            "n_players": [300, 300],
            "spearman": [0.65, 0.85],
        }
    )

    report_path = tmp_path / "docs" / "model-prototype-gbm.md"
    return render_gbm_report(
        train_row_count=1000,
        train_seasons=["2016-17", "2017-18"],
        validation_row_count=300,
        validation_season="2024-25",
        glm_component_metrics=glm_component_metrics,
        lgbm_component_metrics=lgbm_component_metrics,
        glm_points_report=glm_points_report,
        lgbm_points_report=lgbm_points_report,
        glm_gameweek_spearman=glm_gameweek_spearman,
        lgbm_gameweek_spearman=lgbm_gameweek_spearman,
        report_path=report_path,
    )


def test_report_has_every_section(tmp_path: Path) -> None:
    report = _render(tmp_path)
    for heading in (
        "Split summary",
        "Per-component, per-position metrics",
        "System score",
        "Rank correlation by gameweek",
    ):
        assert heading in report


def test_split_row_counts_and_seasons_are_reported(tmp_path: Path) -> None:
    report = _render(tmp_path)
    assert "1000 row(s)" in report
    assert "2016-17, 2017-18" in report
    assert "300 row(s)" in report
    assert "2024-25" in report


def test_component_metrics_interleave_glm_then_lgbm_per_key(tmp_path: Path) -> None:
    report = _render(tmp_path)
    lines = [line for line in report.splitlines() if line.startswith("| minutes")]
    assert len(lines) == 2
    assert "| glm |" in lines[0]
    assert "| lgbm |" in lines[1]


def test_points_report_interleaves_glm_then_lgbm_per_bucket(tmp_path: Path) -> None:
    report = _render(tmp_path)
    lines = [line for line in report.splitlines() if line.startswith("| overall")]
    assert len(lines) == 2
    assert "| glm |" in lines[0]
    assert "| lgbm |" in lines[1]


def test_gameweek_spearman_is_summarised_per_model(tmp_path: Path) -> None:
    report = _render(tmp_path)
    assert "mean_spearman" in report
    assert "0.7000" in report  # glm mean of 0.6 and 0.8
    assert "0.7500" in report  # lgbm mean of 0.65 and 0.85


def test_test_split_is_never_mentioned_as_used(tmp_path: Path) -> None:
    report = _render(tmp_path)
    assert "never read" in report


def test_a_key_present_only_in_glm_still_renders_its_row(tmp_path: Path) -> None:
    glm_only = pl.DataFrame(
        {
            "component": ["minutes", "goals_scored"],
            "position": ["MID", "GK"],
            "mae": [9.0, 0.1],
            "rmse": [14.0, 0.2],
            "poisson_deviance": [None, 0.1],
            "n": [50, 10],
        }
    )
    lgbm_partial = pl.DataFrame(
        {
            "component": ["minutes"],
            "position": ["MID"],
            "mae": [8.5],
            "rmse": [13.0],
            "poisson_deviance": [None],
            "n": [50],
        }
    )

    report = render_gbm_report(
        train_row_count=1000,
        train_seasons=["2016-17"],
        validation_row_count=300,
        validation_season="2024-25",
        glm_component_metrics=glm_only,
        lgbm_component_metrics=lgbm_partial,
        glm_points_report=pl.DataFrame(
            {
                "bucket": ["overall"],
                "mae": [1.0],
                "rmse": [1.5],
                "poisson_deviance": [None],
                "n": [10],
            }
        ),
        lgbm_points_report=pl.DataFrame(
            {
                "bucket": ["overall"],
                "mae": [0.9],
                "rmse": [1.4],
                "poisson_deviance": [None],
                "n": [10],
            }
        ),
        glm_gameweek_spearman=pl.DataFrame(
            {"season": ["2024-25"], "event": [1], "n_players": [300], "spearman": [0.6]}
        ),
        lgbm_gameweek_spearman=pl.DataFrame(
            {"season": ["2024-25"], "event": [1], "n_players": [300], "spearman": [0.7]}
        ),
        report_path=tmp_path / "docs" / "model-prototype-gbm.md",
    )

    goals_scored_lines = [line for line in report.splitlines() if line.startswith("| goals_scored")]
    assert len(goals_scored_lines) == 1
    assert "| glm |" in goals_scored_lines[0]
