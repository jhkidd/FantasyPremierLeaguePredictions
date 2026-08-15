"""Plotting for the Step 25 EDA sweep (Phase A Step 26).

Kept separate from :mod:`fpl.training.eda` so that module stays pure-stats
(polars/numpy/scipy/sklearn only) - matplotlib is a heavier, display-adjacent
dependency this module alone needs. No tests required per the plan; this is
rendering code, not a statistic with a right answer to pin.

Every function writes one or more PNGs under a caller-supplied output
directory (``data/eda/`` in normal use) and returns the path(s) written, so
the CLI (Step 27) can log what it produced without re-deriving filenames.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: CI and the CLI never have a display.

import matplotlib.pyplot as plt  # noqa: E402
import polars as pl  # noqa: E402

__all__ = [
    "OUTCOME_BUCKET_EDGES",
    "OUTCOME_BUCKET_LABELS",
    "plot_correlation_heatmap",
    "plot_feature_histograms",
    "plot_missingness_by_season",
    "plot_target_distribution",
]

# zeros = 0, blanks = 1-3, tickers = 4-8, haulers = 9+ (plan Step 30's own
# bucket definition, reused here so the EDA target-distribution plot already
# shows the buckets the baseline will later be scored against).
OUTCOME_BUCKET_EDGES: tuple[float, ...] = (-0.5, 0.5, 3.5, 8.5)
OUTCOME_BUCKET_LABELS: tuple[str, ...] = ("zeros", "blanks", "tickers", "haulers")


def plot_feature_histograms(frame: pl.DataFrame, columns: list[str], out_dir: Path) -> list[Path]:
    """One histogram PNG per column in ``columns``, each named
    ``hist_<column>.png``. Null values are dropped, not binned, since a
    histogram has no meaningful bar for "missing"."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for column in columns:
        values = frame[column].drop_nulls().to_numpy()
        fig, ax = plt.subplots()
        ax.hist(values, bins=30)
        ax.set_title(f"{column} (n={len(values)})")
        ax.set_xlabel(column)
        ax.set_ylabel("count")
        path = out_dir / f"hist_{column}.png"
        fig.savefig(path)
        plt.close(fig)
        written.append(path)
    return written


def plot_target_distribution(
    frame: pl.DataFrame, target: str, out_dir: Path, *, name: str | None = None
) -> Path:
    """Histogram of ``target``'s realised values, with the zeros/blanks/
    tickers/haulers outcome buckets marked as shaded, labelled bands."""
    out_dir.mkdir(parents=True, exist_ok=True)
    values = frame[target].drop_nulls().to_numpy()

    fig, ax = plt.subplots()
    max_value = max(int(values.max()) if len(values) else 0, int(OUTCOME_BUCKET_EDGES[-1]) + 1)
    ax.hist(values, bins=range(0, max_value + 2), align="left")

    edges = (*OUTCOME_BUCKET_EDGES, max_value + 0.5)
    colours = ("#dddddd", "#ffe5b4", "#b4d8ff", "#ffb4b4")
    for start, end, label, colour in zip(
        edges[:-1], edges[1:], OUTCOME_BUCKET_LABELS, colours, strict=True
    ):
        ax.axvspan(start, end, color=colour, alpha=0.3, label=label)

    ax.set_title(f"{target} distribution (n={len(values)})")
    ax.set_xlabel(target)
    ax.set_ylabel("count")
    ax.legend()

    path = out_dir / f"target_distribution_{name or target}.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def plot_correlation_heatmap(matrix: pl.DataFrame, out_dir: Path, *, name: str = "pearson") -> Path:
    """Heatmap of a ``correlation_matrices``-shaped frame (a ``column``
    label column plus one column per feature)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = matrix["column"].to_list()
    values = matrix.select([c for c in matrix.columns if c != "column"]).to_numpy()

    fig, ax = plt.subplots(figsize=(max(6, len(labels) * 0.3), max(6, len(labels) * 0.3)))
    image = ax.imshow(values, vmin=-1, vmax=1, cmap="coolwarm")
    ax.set_xticks(range(len(labels)), labels, rotation=90, fontsize=6)
    ax.set_yticks(range(len(labels)), labels, fontsize=6)
    ax.set_title(f"{name} correlation")
    fig.colorbar(image, ax=ax)
    fig.tight_layout()

    path = out_dir / f"correlation_heatmap_{name}.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def plot_missingness_by_season(frame: pl.DataFrame, columns: list[str], out_dir: Path) -> Path:
    """Stacked bar chart of null fraction per season, one bar-group per
    column in ``columns`` - the direct way to see an era boundary (a
    column's missingness jumping from 100% to 0% at the season it starts
    being recorded), which a single overall missing-fraction figure hides."""
    out_dir.mkdir(parents=True, exist_ok=True)
    seasons = sorted(frame["season"].unique().to_list())

    fig, ax = plt.subplots(figsize=(max(8, len(seasons) * 0.6), 6))
    width = 0.8 / max(len(columns), 1)
    x = range(len(seasons))
    for i, column in enumerate(columns):
        fractions = []
        for season in seasons:
            season_frame = frame.filter(pl.col("season") == season)
            fractions.append(
                season_frame[column].null_count() / season_frame.height
                if season_frame.height
                else 0
            )
        offsets = [xi + i * width for xi in x]
        ax.bar(offsets, fractions, width=width, label=column)

    ax.set_xticks([xi + width * len(columns) / 2 for xi in x], seasons, rotation=45)
    ax.set_ylabel("null fraction")
    ax.set_title("Missingness by season")
    ax.legend(fontsize=6)
    fig.tight_layout()

    path = out_dir / "missingness_by_season.png"
    fig.savefig(path)
    plt.close(fig)
    return path
