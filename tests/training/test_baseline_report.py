"""Tests for :mod:`fpl.training.baseline_report` (Phase A Step 31)."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from fpl.training.baseline_report import render_baseline_report


def _render(tmp_path: Path) -> str:
    naive_metrics = pl.DataFrame(
        {
            "component": ["minutes", "goals_scored"],
            "mae": [10.0, 0.2],
            "rmse": [15.0, 0.4],
            "n": [100, 100],
        }
    )
    glm_metrics = pl.DataFrame(
        {
            "component": ["minutes", "goals_scored"],
            "position": ["MID", "MID"],
            "mae": [9.0, 0.15],
            "rmse": [14.0, 0.3],
            "poisson_deviance": [None, 0.5],
            "n": [50, 50],
        }
    )
    points_report = pl.DataFrame(
        {
            "bucket": ["overall", "zeros", "blanks", "tickers", "haulers"],
            "mae": [1.5, 0.5, 1.0, 2.0, 3.0],
            "rmse": [2.0, 0.6, 1.2, 2.5, 3.5],
            "poisson_deviance": [None, None, None, None, None],
            "n": [100, 20, 40, 30, 10],
        }
    )
    gameweek_spearman = pl.DataFrame(
        {
            "season": ["2024-25", "2024-25"],
            "event": [1, 2],
            "n_players": [300, 300],
            "spearman": [0.6, 0.8],
        }
    )
    era_continuity_metrics = pl.DataFrame(
        {
            "group": ["overall", "DEF"],
            "model": ["glm", "glm"],
            "mae": [1.0, 0.8],
            "rmse": [1.5, 1.2],
            "poisson_deviance": [0.9, 0.7],
            "n": [500, 200],
        }
    )

    report_path = tmp_path / "docs" / "model-prototype-baseline.md"
    return render_baseline_report(
        train_row_count=1000,
        train_seasons=["2016-17", "2017-18"],
        validation_row_count=300,
        validation_season="2024-25",
        naive_metrics=naive_metrics,
        glm_metrics=glm_metrics,
        points_report=points_report,
        gameweek_spearman=gameweek_spearman,
        era_continuity_metrics=era_continuity_metrics,
        report_path=report_path,
    )


def test_report_has_every_section(tmp_path: Path) -> None:
    report = _render(tmp_path)
    for heading in (
        "Split summary",
        "Naive baseline",
        "GLM baseline",
        "System score",
        "Rank correlation by gameweek",
        "Defensive-contribution era-continuity experiment",
    ):
        assert heading in report


def test_split_row_counts_and_seasons_are_reported(tmp_path: Path) -> None:
    report = _render(tmp_path)
    assert "1000 row(s)" in report
    assert "2016-17, 2017-18" in report
    assert "300 row(s)" in report
    assert "2024-25" in report


def test_gameweek_spearman_is_summarised_not_listed_row_by_row(tmp_path: Path) -> None:
    report = _render(tmp_path)
    assert "mean_spearman" in report
    assert "0.7000" in report  # mean of 0.6 and 0.8


def test_test_split_is_never_mentioned_as_used(tmp_path: Path) -> None:
    report = _render(tmp_path)
    assert "sanctioned one-time exception" in report


def test_era_continuity_table_is_rendered(tmp_path: Path) -> None:
    report = _render(tmp_path)
    assert "| group | model |" in report
    assert "| overall | glm |" in report
