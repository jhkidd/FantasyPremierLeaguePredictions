"""Markdown report generation for the Phase B LightGBM-vs-GLM comparison
(`.github/context/gbm-baseline-phase-b.md` step 5).

Pure string assembly over already-computed metric frames, mirroring
:mod:`fpl.training.baseline_report`'s own split between models, metrics,
and rendering. The one structural difference: every table here interleaves
a GLM row with its matching LightGBM row (same component/position, or same
outcome bucket) via a ``model`` column, so the two are always adjacent -
directly comparable without cross-referencing two separate reports.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import polars as pl

__all__ = ["render_gbm_report"]


def _markdown_table(frame: pl.DataFrame, *, float_places: int = 4) -> str:
    if frame.height == 0:
        return "_(no rows)_\n"
    header = "| " + " | ".join(frame.columns) + " |"
    separator = "| " + " | ".join("---" for _ in frame.columns) + " |"
    lines = [header, separator]
    for row in frame.iter_rows():
        cells = []
        for value in row:
            if value is None:
                cells.append("")
            elif isinstance(value, float):
                cells.append(f"{value:.{float_places}f}")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _interleave_by_model(
    glm_frame: pl.DataFrame, lgbm_frame: pl.DataFrame, *, keys: Sequence[str]
) -> pl.DataFrame:
    """Combine two metrics frames sharing the same ``keys`` columns (e.g.
    ``["component", "position"]`` or ``["bucket"]``) into one table with a
    new ``model`` column, walking ``glm_frame``'s own row order and
    emitting its ``"glm"`` row immediately followed by the matching
    ``"lgbm"`` row from ``lgbm_frame`` (if any) for that same key - so
    scanning the rendered table top to bottom always sees the two models'
    numbers for the same thing side by side, never in two separate
    tables a reader has to cross-reference.

    A key present in ``glm_frame`` but absent from ``lgbm_frame`` (e.g. the
    GLM baseline fit a (component, position) pair the GBM training split
    happened not to) still emits its ``"glm"`` row alone, since a missing
    comparison point is not a reason to hide the one number that does
    exist.
    """
    value_columns = [column for column in glm_frame.columns if column not in keys]
    lgbm_lookup = {tuple(row[key] for key in keys): row for row in lgbm_frame.iter_rows(named=True)}

    rows: list[dict] = []
    for glm_row in glm_frame.iter_rows(named=True):
        key = tuple(glm_row[k] for k in keys)
        rows.append(
            {
                **{k: glm_row[k] for k in keys},
                "model": "glm",
                **{c: glm_row[c] for c in value_columns},
            }
        )
        lgbm_row = lgbm_lookup.get(key)
        if lgbm_row is not None:
            rows.append(
                {
                    **{k: glm_row[k] for k in keys},
                    "model": "lgbm",
                    **{c: lgbm_row[c] for c in value_columns},
                }
            )
    return pl.DataFrame(rows)


def _spearman_summary(gameweek_spearman: pl.DataFrame, *, model: str) -> dict:
    values = gameweek_spearman["spearman"].drop_nulls()
    return {
        "model": model,
        "gameweeks_scored": values.len(),
        "mean_spearman": values.mean(),
        "median_spearman": values.median(),
        "min_spearman": values.min(),
        "max_spearman": values.max(),
    }


def render_gbm_report(
    *,
    train_row_count: int,
    train_seasons: Sequence[str],
    validation_row_count: int,
    validation_season: str,
    glm_component_metrics: pl.DataFrame,
    lgbm_component_metrics: pl.DataFrame,
    glm_points_report: pl.DataFrame,
    lgbm_points_report: pl.DataFrame,
    glm_gameweek_spearman: pl.DataFrame,
    lgbm_gameweek_spearman: pl.DataFrame,
    report_path: Path,
) -> str:
    """Render the Phase B GLM-vs-LightGBM comparison markdown report as a
    string.

    ``glm_component_metrics``/``lgbm_component_metrics``: one row per
    ``(component, position)`` (``mae``, ``rmse``, ``poisson_deviance``,
    ``n``) each - the same per-(component, position) shape
    :mod:`fpl.training.baseline_report`'s own ``glm_metrics`` uses,
    computed once per model.

    ``glm_points_report``/``lgbm_points_report``:
    :func:`fpl.training.evaluation.points_error_report`'s output for each
    model - one row for ``"overall"`` plus one per outcome bucket.

    ``glm_gameweek_spearman``/``lgbm_gameweek_spearman``:
    :func:`fpl.training.evaluation.spearman_by_gameweek`'s output for each
    model, summarised here as mean/median/min/max over every scored
    gameweek per model.

    Every metric in this report is computed on the validation split only
    (mirroring :mod:`fpl.training.baseline_report`'s own leakage boundary);
    this report never reads the test split at all.
    """
    component_comparison = _interleave_by_model(
        glm_component_metrics, lgbm_component_metrics, keys=["component", "position"]
    )
    points_comparison = _interleave_by_model(glm_points_report, lgbm_points_report, keys=["bucket"])
    spearman_comparison = pl.DataFrame(
        [
            _spearman_summary(glm_gameweek_spearman, model="glm"),
            _spearman_summary(lgbm_gameweek_spearman, model="lgbm"),
        ]
    )

    sections = [
        "# Phase B — LightGBM vs. GLM baseline (validation split)",
        "",
        f"Generated by `fpl gbm-baseline`. This file is regenerated from scratch on "
        f"every run — do not hand-edit. Report path: `{report_path.name}`. See "
        "`.github/context/gbm-baseline-phase-b.md` for the task this compares.",
        "",
        "## 1. Split summary",
        "",
        f"- Training split: {train_row_count} row(s) across season(s) "
        f"{', '.join(train_seasons) if train_seasons else '(none)'}.",
        f"- Validation split: {validation_row_count} row(s), season {validation_season}.",
        "- The test split is never read anywhere in this report.",
        "",
        "## 2. Per-component, per-position metrics — GLM vs. LightGBM (validation split)",
        "",
        "Both models share the same two-stage design (a `minutes` model per position, a "
        "component model per (component, position) fit only on rows the player actually "
        "played) and the same feature set *except* the GLM baseline excludes era-masked "
        "rolling columns that the LightGBM model keeps (`.github/context/"
        "gbm-baseline-phase-b.md`). Each `(component, position)` pair's `glm` row is "
        "immediately followed by its `lgbm` row for direct comparison.",
        "",
        _markdown_table(component_comparison),
        "## 3. System score — predicted vs realised total points (validation split)",
        "",
        "Every predicted component combined through that row's own season's scoring "
        "ruleset (`fpl.training.evaluation.assemble_predicted_points`), overall and "
        "within each outcome bucket (bucketed by the *realised* total).",
        "",
        _markdown_table(points_comparison),
        "## 4. Rank correlation by gameweek (validation split)",
        "",
        "Spearman rank correlation between predicted and realised total points, computed "
        "within each gameweek's player pool, then summarised across every scored gameweek, "
        "per model.",
        "",
        _markdown_table(spearman_comparison),
    ]
    return "\n".join(sections) + "\n"
