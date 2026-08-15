"""Data-evaluation sweep for the training matrix (Phase A Step 25).

Every function here must be called **on the training split only**
(:func:`fpl.training.splits.chronological_split`'s first element) — computing
any of this over validation or test rows would leak information about data
the eventual model selection is supposed to never have seen.

Column *kinds*, used throughout to decide which report each column belongs
in:

- ``identifier`` — ``season``, ``event``, ``fixture_id``, ``player_id``,
  ``player_code``. Never a feature; excluded from every statistical report.
- ``categorical`` — ``position``, ``team_code``, ``opponent_team_code``.
  Nominal, not ordinal — this schema currently has no ordinal column, but
  the ``kind`` value exists for one to classify into later.
- ``boolean`` — ``was_home`` and the four ``obs_*`` masks.
- ``label`` — the 13 ``label_*`` target columns.
- ``numeric`` — every other column (the rolling-window and team-context
  features), the only kind analysed for variance/skew/outliers/correlation.

Every numeric-column function accepts an explicit ``columns`` override so
:func:`run_eda_sweep` can bound expensive computations (:func:`vif_report`,
:func:`mutual_information_report`) to a tractable subset without touching
the rest of this module's contract.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import polars as pl
from scipy import stats as scipy_stats
from sklearn.feature_selection import mutual_info_regression
from sklearn.linear_model import LinearRegression

from fpl.training.dataset import IDENTITY_COLUMNS, LABEL_COLUMNS, OBS_COLUMNS

__all__ = [
    "EdaResult",
    "classify_columns",
    "correlation_matrices",
    "distribution_report",
    "high_correlation_pairs",
    "missing_value_report",
    "mutual_information_report",
    "numeric_feature_columns",
    "outlier_report",
    "run_eda_sweep",
    "target_correlation_report",
    "variance_report",
    "vif_report",
]

_IDENTIFIER_COLUMNS = frozenset({"season", "event", "fixture_id", "player_id", "player_code"})
_CATEGORICAL_COLUMNS = frozenset({"position", "team_code", "opponent_team_code"})
_BOOLEAN_COLUMNS = frozenset({"was_home", *OBS_COLUMNS})
_LABEL_COLUMNS = frozenset(LABEL_COLUMNS)

assert _IDENTIFIER_COLUMNS | _CATEGORICAL_COLUMNS | {"was_home"} == set(IDENTITY_COLUMNS)


def classify_columns(frame: pl.DataFrame) -> pl.DataFrame:
    """One row per column of ``frame``: ``column``, ``kind``."""
    rows = []
    for column in frame.columns:
        if column in _IDENTIFIER_COLUMNS:
            kind = "identifier"
        elif column in _CATEGORICAL_COLUMNS:
            kind = "categorical"
        elif column in _BOOLEAN_COLUMNS:
            kind = "boolean"
        elif column in _LABEL_COLUMNS:
            kind = "label"
        else:
            kind = "numeric"
        rows.append({"column": column, "kind": kind})
    return pl.DataFrame(rows, schema={"column": pl.Utf8, "kind": pl.Utf8})


def numeric_feature_columns(frame: pl.DataFrame) -> list[str]:
    """Every column classified ``numeric`` — the only kind that variance,
    skew, outlier, correlation, VIF and mutual-information reports cover."""
    kinds = classify_columns(frame)
    return kinds.filter(pl.col("kind") == "numeric")["column"].to_list()


def cardinality_report(frame: pl.DataFrame) -> pl.DataFrame:
    """``n_unique`` for every ``identifier``/``categorical``/``boolean``
    column — the kinds an encoding decision applies to."""
    kinds = classify_columns(frame)
    columns = kinds.filter(pl.col("kind").is_in(["identifier", "categorical", "boolean"]))[
        "column"
    ].to_list()
    rows = [{"column": column, "n_unique": frame[column].n_unique()} for column in columns]
    return pl.DataFrame(rows, schema={"column": pl.Utf8, "n_unique": pl.UInt32})


def missing_value_report(frame: pl.DataFrame) -> pl.DataFrame:
    """``null_count``/``null_fraction`` for every column in ``frame``.

    For a rolling feature derived from one of :data:`fpl.features.rolling
    <fpl.features.rolling.MASKED_COLUMN_GROUPS>`'s masked groups, also report
    ``null_fraction_when_observed``/``null_fraction_when_not_observed``,
    split by *that row's own* governing ``obs_*`` flag. This is a documented
    approximation, not an exact accounting of each window's composition (a
    window can straddle an era boundary) — but a row's own ``obs_*`` value is
    strongly correlated with its season's era, so it is a good proxy for
    "was this null because history hadn't started yet, or because this
    era never recorded the stat" without threading per-window era
    provenance through every feature.
    """
    height = frame.height
    obs_column_by_feature = _masked_feature_obs_column(frame)
    filtered_true = {
        obs_column: frame.filter(pl.col(obs_column))
        for obs_column in set(obs_column_by_feature.values())
    }
    filtered_false = {
        obs_column: frame.filter(~pl.col(obs_column))
        for obs_column in set(obs_column_by_feature.values())
    }

    rows = []
    for column in frame.columns:
        null_count = frame[column].null_count()
        row: dict[str, object] = {
            "column": column,
            "null_count": null_count,
            "null_fraction": (null_count / height) if height else None,
            "obs_column": obs_column_by_feature.get(column),
            "null_fraction_when_observed": None,
            "null_fraction_when_not_observed": None,
        }
        obs_column = obs_column_by_feature.get(column)
        if obs_column is not None:
            observed = filtered_true[obs_column]
            not_observed = filtered_false[obs_column]
            if observed.height:
                row["null_fraction_when_observed"] = observed[column].null_count() / observed.height
            if not_observed.height:
                row["null_fraction_when_not_observed"] = (
                    not_observed[column].null_count() / not_observed.height
                )
        rows.append(row)
    return pl.DataFrame(rows)


def _masked_feature_obs_column(frame: pl.DataFrame) -> dict[str, str]:
    """Map every present rolling-feature column derived from a masked
    group back to its governing ``obs_*`` column, by reconstructing
    :mod:`fpl.features.rolling`'s own naming scheme."""
    from fpl.features.rolling import FIXTURE_WINDOWS, MASKED_COLUMN_GROUPS

    labels = (*(str(w) for w in FIXTURE_WINDOWS), "season_to_date", "last_season")
    mapping: dict[str, str] = {}
    for obs_column, base_columns in MASKED_COLUMN_GROUPS:
        for base_column in base_columns:
            for label in labels:
                for suffix in ("sum", "per90"):
                    candidate = f"{base_column}_{suffix}_last_{label}"
                    if candidate in frame.columns:
                        mapping[candidate] = obs_column
    return mapping


def variance_report(frame: pl.DataFrame, *, near_zero_threshold: float = 1e-8) -> pl.DataFrame:
    """``variance`` and a ``near_zero_variance`` flag for every numeric
    feature column. A feature whose variance falls below
    ``near_zero_threshold`` carries almost no information and is a
    candidate to drop before modelling."""
    rows = []
    for column in numeric_feature_columns(frame):
        values = frame[column].drop_nulls()
        variance = values.var() if values.len() > 1 else None
        rows.append(
            {
                "column": column,
                "variance": variance,
                "near_zero_variance": variance is not None and variance < near_zero_threshold,
            }
        )
    return pl.DataFrame(
        rows, schema={"column": pl.Utf8, "variance": pl.Float64, "near_zero_variance": pl.Boolean}
    )


def distribution_report(frame: pl.DataFrame) -> pl.DataFrame:
    """``skewness``/``kurtosis`` (Fisher, excess) for every numeric feature
    column, computed over its non-null values.

    Many real feature columns (a rarely-occurring count stat such as
    ``red_cards`` or ``own_goals``) are near-constant — almost every value
    is the same number. scipy's skew/kurtosis still return a value for
    that, but warn about "catastrophic cancellation" precision loss, which
    the project's ``filterwarnings = ["error"]`` test setting would
    otherwise turn into a crash. That warning is expected here, not a sign
    of a wrong result for a diagnostic statistic, so it is suppressed only
    for this call.
    """
    rows = []
    for column in numeric_feature_columns(frame):
        values = frame[column].drop_nulls().to_numpy()
        if values.size < 3:
            rows.append({"column": column, "skewness": None, "kurtosis": None})
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            skewness = float(scipy_stats.skew(values))
            kurtosis = float(scipy_stats.kurtosis(values))
        rows.append({"column": column, "skewness": skewness, "kurtosis": kurtosis})
    return pl.DataFrame(
        rows, schema={"column": pl.Utf8, "skewness": pl.Float64, "kurtosis": pl.Float64}
    )


def outlier_report(
    frame: pl.DataFrame, *, iqr_multiplier: float = 1.5, zscore_threshold: float = 3.0
) -> pl.DataFrame:
    """Outlier counts for every numeric feature column by two independent
    rules: outside ``[Q1 - iqr_multiplier*IQR, Q3 + iqr_multiplier*IQR]``,
    and ``|z-score| > zscore_threshold``. Both are standard, conventional
    default thresholds — reported side by side since they disagree on
    skewed distributions and neither alone is authoritative."""
    rows = []
    for column in numeric_feature_columns(frame):
        values = frame[column].drop_nulls()
        n = values.len()
        if n == 0:
            rows.append(
                {
                    "column": column,
                    "iqr_outlier_count": 0,
                    "iqr_outlier_fraction": None,
                    "zscore_outlier_count": 0,
                    "zscore_outlier_fraction": None,
                }
            )
            continue

        q1 = values.quantile(0.25)
        q3 = values.quantile(0.75)
        iqr = (q3 - q1) if (q1 is not None and q3 is not None) else None
        if iqr is not None:
            lower, upper = q1 - iqr_multiplier * iqr, q3 + iqr_multiplier * iqr
            iqr_outliers = values.filter((values < lower) | (values > upper)).len()
        else:
            iqr_outliers = 0

        std = values.std()
        if std and std > 0:
            mean = values.mean()
            z = ((values - mean) / std).abs()
            zscore_outliers = z.filter(z > zscore_threshold).len()
        else:
            zscore_outliers = 0

        rows.append(
            {
                "column": column,
                "iqr_outlier_count": iqr_outliers,
                "iqr_outlier_fraction": iqr_outliers / n,
                "zscore_outlier_count": zscore_outliers,
                "zscore_outlier_fraction": zscore_outliers / n,
            }
        )
    return pl.DataFrame(rows)


def _pairwise_correlation(matrix: np.ndarray) -> np.ndarray:
    """Pairwise-complete correlation: each pair of columns is correlated
    over only the rows where *both* are non-NaN, rather than list-wise
    dropping any row with a NaN in *any* column (which would drop almost
    every row, since early-history rows are systematically null in many
    columns at once)."""
    masked = np.ma.masked_invalid(matrix)
    result = np.ma.corrcoef(masked, rowvar=False)
    return np.ma.filled(result, np.nan)


def _rank_transform(matrix: np.ndarray) -> np.ndarray:
    """Rank each column independently over its own non-NaN values, leaving
    NaN in place elsewhere — the standard "Spearman is Pearson-on-ranks"
    reduction, applied per column so one column's nulls never affect
    another's ranking."""
    ranked = np.full_like(matrix, np.nan, dtype=np.float64)
    for j in range(matrix.shape[1]):
        column = matrix[:, j]
        finite = np.isfinite(column)
        if finite.any():
            ranked[finite, j] = scipy_stats.rankdata(column[finite])
    return ranked


def correlation_matrices(
    frame: pl.DataFrame, *, columns: list[str] | None = None
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Pairwise-complete Pearson and Spearman correlation matrices over
    ``columns`` (default: every numeric feature column), each returned as a
    square frame with a leading ``column`` label column."""
    selected = columns if columns is not None else numeric_feature_columns(frame)
    raw = frame.select(selected).to_numpy().astype(np.float64)

    pearson = _pairwise_correlation(raw)
    spearman = _pairwise_correlation(_rank_transform(raw))

    def _to_frame(values: np.ndarray) -> pl.DataFrame:
        out = pl.DataFrame(values, schema=selected)
        return out.insert_column(0, pl.Series("column", selected))

    return _to_frame(pearson), _to_frame(spearman)


def high_correlation_pairs(matrix: pl.DataFrame, *, threshold: float = 0.9) -> pl.DataFrame:
    """Every unordered feature pair with ``|r| > threshold`` from a
    ``correlation_matrices``-shaped frame, one row each, sorted by
    descending ``abs(r)``. Flags only — per Step 27, nothing is dropped
    here."""
    columns = [c for c in matrix.columns if c != "column"]
    labels = matrix["column"].to_list()
    rows = []
    for i, row_label in enumerate(labels):
        for j in range(i + 1, len(columns)):
            r = matrix[i, columns[j]]
            if r is not None and abs(r) > threshold:
                rows.append({"feature_a": row_label, "feature_b": columns[j], "r": r})
    result = pl.DataFrame(
        rows, schema={"feature_a": pl.Utf8, "feature_b": pl.Utf8, "r": pl.Float64}
    )
    if result.height:
        result = (
            result.with_columns(pl.col("r").abs().alias("_abs_r"))
            .sort("_abs_r", descending=True)
            .drop("_abs_r")
        )
    return result


def _sample_rows(frame: pl.DataFrame, *, sample_size: int, seed: int = 0) -> pl.DataFrame:
    if frame.height <= sample_size:
        return frame
    return frame.sample(n=sample_size, seed=seed)


def vif_report(
    frame: pl.DataFrame, *, columns: list[str] | None = None, sample_size: int = 5000
) -> pl.DataFrame:
    """Variance inflation factor for ``columns`` (default: every numeric
    feature column), ``1 / (1 - R^2)`` from regressing each column on every
    other selected column. Rows with a null in any selected column are
    dropped (VIF needs a common design matrix, unlike the pairwise
    correlation functions above) and, on a large frame, further subsampled
    to ``sample_size`` rows — this is a diagnostic estimate, not a modelling
    input, so an exact figure over every row is not worth the O(columns^2 x
    rows) cost. Pass a curated, non-redundant ``columns`` subset for a
    tractable result on the full ~470-column feature set; the unrestricted
    default is appropriate only for a smaller column list."""
    selected = columns if columns is not None else numeric_feature_columns(frame)
    complete = _sample_rows(frame.select(selected).drop_nulls(), sample_size=sample_size)
    if complete.height == 0 or len(selected) < 2:
        return pl.DataFrame({"column": selected, "vif": [None] * len(selected)})

    data = complete.to_numpy().astype(np.float64)
    rows = []
    for i, column in enumerate(selected):
        y = data[:, i]
        x = np.delete(data, i, axis=1)
        r_squared = LinearRegression().fit(x, y).score(x, y)
        vif = float("inf") if r_squared >= 1.0 else 1.0 / (1.0 - r_squared)
        rows.append({"column": column, "vif": vif})
    return pl.DataFrame(rows, schema={"column": pl.Utf8, "vif": pl.Float64})


def mutual_information_report(
    frame: pl.DataFrame,
    targets: list[str] | None = None,
    *,
    columns: list[str] | None = None,
    sample_size: int = 5000,
    seed: int = 0,
) -> pl.DataFrame:
    """Mutual information between every selected numeric feature and every
    ``target`` label column (default: :data:`fpl.training.dataset.LABEL_COLUMNS`),
    catching non-linear relationships correlation misses. Rows with a null
    feature are imputed with that column's median (mutual information needs
    a value for every row; the median is only used for this diagnostic, never
    for modelling), and the frame is subsampled to ``sample_size`` rows on a
    large input for the same tractability reason as :func:`vif_report`."""
    selected = columns if columns is not None else numeric_feature_columns(frame)
    target_columns = targets if targets is not None else list(LABEL_COLUMNS)
    sampled = _sample_rows(frame, sample_size=sample_size, seed=seed)

    feature_matrix = sampled.select(selected).to_numpy().astype(np.float64)
    with warnings.catch_warnings():
        # A curated column can be entirely null in a small/early sample (e.g.
        # a team-context feature before any facts/team_fixture exists yet),
        # which makes its column-wise median NaN. The following
        # `np.nan_to_num` call maps that NaN median back to 0.0 as the fill
        # value, which is a safe, information-free treatment for a column
        # that already carries no signal in this sample.
        warnings.simplefilter("ignore", category=RuntimeWarning)
        column_medians = np.nanmedian(feature_matrix, axis=0)
    nan_mask = np.isnan(feature_matrix)
    feature_matrix = np.where(
        nan_mask, np.broadcast_to(column_medians, feature_matrix.shape), feature_matrix
    )
    feature_matrix = np.nan_to_num(feature_matrix, nan=0.0)

    rows = []
    for target in target_columns:
        target_values = sampled[target].to_numpy().astype(np.float64)
        valid = ~np.isnan(target_values)
        if valid.sum() < 2:
            rows.extend(
                {"feature": f, "target": target, "mutual_information": None} for f in selected
            )
            continue
        mi = mutual_info_regression(feature_matrix[valid], target_values[valid], random_state=seed)
        rows.extend(
            {"feature": f, "target": target, "mutual_information": float(value)}
            for f, value in zip(selected, mi, strict=True)
        )
    return pl.DataFrame(
        rows, schema={"feature": pl.Utf8, "target": pl.Utf8, "mutual_information": pl.Float64}
    )


def target_correlation_report(
    frame: pl.DataFrame, targets: list[str] | None = None, *, columns: list[str] | None = None
) -> pl.DataFrame:
    """Pearson and Spearman correlation of every selected numeric feature
    against every ``target`` label column (default:
    :data:`fpl.training.dataset.LABEL_COLUMNS`), pairwise-complete over each
    feature/target pair independently, sorted within each target by
    descending ``abs(pearson_r)``."""
    selected = columns if columns is not None else numeric_feature_columns(frame)
    target_columns = targets if targets is not None else list(LABEL_COLUMNS)

    rows = []
    for target in target_columns:
        target_values = frame[target].to_numpy().astype(np.float64)
        for feature in selected:
            feature_values = frame[feature].to_numpy().astype(np.float64)
            valid = np.isfinite(target_values) & np.isfinite(feature_values)
            if (
                valid.sum() < 2
                or np.std(feature_values[valid]) == 0
                or np.std(target_values[valid]) == 0
            ):
                rows.append(
                    {"feature": feature, "target": target, "pearson_r": None, "spearman_r": None}
                )
                continue
            pearson_r = float(np.corrcoef(feature_values[valid], target_values[valid])[0, 1])
            spearman_r = float(
                scipy_stats.spearmanr(feature_values[valid], target_values[valid]).statistic
            )
            rows.append(
                {
                    "feature": feature,
                    "target": target,
                    "pearson_r": pearson_r,
                    "spearman_r": spearman_r,
                }
            )
    result = pl.DataFrame(
        rows,
        schema={
            "feature": pl.Utf8,
            "target": pl.Utf8,
            "pearson_r": pl.Float64,
            "spearman_r": pl.Float64,
        },
    )
    return (
        result.with_columns(pl.col("pearson_r").abs().alias("_abs_r"))
        .sort(["target", "_abs_r"], descending=[False, True])
        .drop("_abs_r")
    )


@dataclass(frozen=True)
class EdaResult:
    """Every Step 25 statistic, bundled for Step 26 (plotting) and Step 27
    (the CLI + markdown report) to consume without recomputing anything."""

    column_kinds: pl.DataFrame
    missing: pl.DataFrame
    cardinality: pl.DataFrame
    variance: pl.DataFrame
    distribution: pl.DataFrame
    outliers: pl.DataFrame
    pearson: pl.DataFrame
    spearman: pl.DataFrame
    high_correlation_pairs: pl.DataFrame
    vif: pl.DataFrame
    mutual_information: pl.DataFrame
    target_correlation: pl.DataFrame


def run_eda_sweep(
    train_frame: pl.DataFrame,
    *,
    vif_columns: list[str] | None = None,
    correlation_threshold: float = 0.9,
    sample_size: int = 5000,
) -> EdaResult:
    """Run every Step 25 statistic over ``train_frame`` (the training split
    only) and bundle the results.

    ``vif_columns`` bounds :func:`vif_report`'s cost — pass a curated subset
    (e.g. one window per stat) for the full feature set; omitted, it falls
    back to every numeric feature column, which is only tractable for a
    small matrix (such as in tests)."""
    pearson, spearman = correlation_matrices(train_frame)
    return EdaResult(
        column_kinds=classify_columns(train_frame),
        missing=missing_value_report(train_frame),
        cardinality=cardinality_report(train_frame),
        variance=variance_report(train_frame),
        distribution=distribution_report(train_frame),
        outliers=outlier_report(train_frame),
        pearson=pearson,
        spearman=spearman,
        high_correlation_pairs=high_correlation_pairs(pearson, threshold=correlation_threshold),
        vif=vif_report(train_frame, columns=vif_columns, sample_size=sample_size),
        mutual_information=mutual_information_report(train_frame, sample_size=sample_size),
        target_correlation=target_correlation_report(train_frame),
    )
