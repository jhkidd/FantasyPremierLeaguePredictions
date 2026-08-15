"""Tests for :mod:`fpl.training.eda_report` (Phase A Step 27)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

from fpl.training.dataset import LABEL_COLUMNS, OBS_COLUMNS
from fpl.training.eda import run_eda_sweep
from fpl.training.eda_report import render_eda_report


def _matrix(n: int, *, seed: int = 0) -> pl.DataFrame:
    """Mirrors ``tests/training/test_eda.py``'s synthetic fixture: a
    perfectly-correlated pair (``feature_b = 2 * feature_a``) so the
    high-correlation-pairs section has something real to list."""
    rng = np.random.default_rng(seed)
    base = rng.normal(size=n)

    columns: dict[str, object] = {
        "season": ["2016-17"] * n,
        "event": list(range(1, n + 1)),
        "fixture_id": list(range(1, n + 1)),
        "player_id": [1] * n,
        "player_code": ["code-1"] * n,
        "position": ["MID"] * n,
        "was_home": [True] * n,
        "team_code": [1] * n,
        "opponent_team_code": [2] * n,
    }
    for obs_column in OBS_COLUMNS:
        columns[obs_column] = [True] * n
    for label in LABEL_COLUMNS:
        columns[label] = (base * 2 + rng.normal(scale=0.1, size=n)).tolist()
    columns["feature_a"] = base.tolist()
    columns["feature_b"] = (base * 2).tolist()
    columns["feature_c"] = rng.normal(size=n).tolist()

    return pl.DataFrame(columns)


def _render(tmp_path: Path) -> str:
    frame = _matrix(200)
    result = run_eda_sweep(frame, vif_columns=["feature_a", "feature_c"])
    report_path = tmp_path / "docs" / "model-prototype-eda.md"
    eda_dir = tmp_path / "data" / "eda"
    return render_eda_report(
        result,
        train_row_count=frame.height,
        train_seasons=["2016-17"],
        curated_columns=["feature_a", "feature_c"],
        histogram_paths=[eda_dir / "hist_feature_a.png", eda_dir / "hist_feature_c.png"],
        target_distribution_paths={
            label: eda_dir / f"target_distribution_{label}.png" for label in LABEL_COLUMNS
        },
        correlation_heatmap_paths={
            "pearson": eda_dir / "correlation_heatmap_pearson.png",
            "spearman": eda_dir / "correlation_heatmap_spearman.png",
        },
        missingness_path=eda_dir / "missingness_by_season.png",
        report_path=report_path,
    )


def test_report_has_every_step_25_section(tmp_path: Path) -> None:
    report = _render(tmp_path)
    for heading in (
        "Feature type classification",
        "Missing values",
        "Cardinality",
        "Variance and near-zero-variance",
        "Skewness and kurtosis",
        "Outliers",
        "Correlation matrices and high-correlation pairs",
        "Variance inflation factor",
        "Mutual information against each target",
        "Target-correlation rankings",
        "Figures",
    ):
        assert heading in report


def test_high_correlation_pair_is_flagged(tmp_path: Path) -> None:
    report = _render(tmp_path)
    assert "feature_a" in report
    assert "feature_b" in report
    # The perfectly-correlated pair must appear in the high-correlation
    # section, not merely somewhere incidental in the report.
    section = report.split("## 7. Correlation matrices")[1].split("## 8.")[0]
    assert "feature_a" in section and "feature_b" in section


def test_image_links_are_relative_to_report_directory(tmp_path: Path) -> None:
    report = _render(tmp_path)
    # report is at tmp_path/docs/model-prototype-eda.md, images at
    # tmp_path/data/eda/... - the relative path must cross up and over.
    assert "../data/eda/hist_feature_a.png" in report


def test_every_target_gets_its_own_subsection(tmp_path: Path) -> None:
    report = _render(tmp_path)
    for label in LABEL_COLUMNS:
        assert f"### {label}" in report
