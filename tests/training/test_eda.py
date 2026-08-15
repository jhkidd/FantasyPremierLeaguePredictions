"""Tests for :mod:`fpl.training.eda` (Phase A Step 25)."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from fpl.training.dataset import IDENTITY_COLUMNS, LABEL_COLUMNS, OBS_COLUMNS
from fpl.training.eda import (
    cardinality_report,
    classify_columns,
    correlation_matrices,
    distribution_report,
    high_correlation_pairs,
    missing_value_report,
    mutual_information_report,
    numeric_feature_columns,
    outlier_report,
    run_eda_sweep,
    target_correlation_report,
    variance_report,
    vif_report,
)


def _matrix(n: int, *, seed: int = 0) -> pl.DataFrame:
    """A synthetic matrix-shaped frame: identity/label/obs columns filled
    with plausible constants, plus a handful of numeric feature columns with
    known relationships for the correlation/VIF/MI tests to pin against."""
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

    # Masked-group feature, governed by obs_defensive.
    columns["cbi_sum_last_3"] = base.tolist()
    # Perfectly correlated pair (feature_b = 2 * feature_a) to pin
    # high_correlation_pairs/VIF against a known-collinear case.
    columns["feature_a"] = base.tolist()
    columns["feature_b"] = (base * 2).tolist()
    # An independent feature, uncorrelated with the rest.
    columns["feature_c"] = rng.normal(size=n).tolist()

    return pl.DataFrame(columns)


class TestClassifyColumns:
    def test_every_identity_column_is_identifier_categorical_or_boolean(self) -> None:
        frame = _matrix(10)
        kinds = classify_columns(frame)
        for column in IDENTITY_COLUMNS:
            kind = kinds.filter(pl.col("column") == column)["kind"][0]
            assert kind in ("identifier", "categorical", "boolean")

    def test_obs_columns_and_labels_classified_correctly(self) -> None:
        frame = _matrix(10)
        kinds = classify_columns(frame)
        for column in OBS_COLUMNS:
            assert kinds.filter(pl.col("column") == column)["kind"][0] == "boolean"
        for column in LABEL_COLUMNS:
            assert kinds.filter(pl.col("column") == column)["kind"][0] == "label"

    def test_feature_columns_classified_numeric(self) -> None:
        frame = _matrix(10)
        assert set(numeric_feature_columns(frame)) == {
            "cbi_sum_last_3",
            "feature_a",
            "feature_b",
            "feature_c",
        }


class TestCardinalityReport:
    def test_reports_unique_counts(self) -> None:
        frame = _matrix(10)
        report = cardinality_report(frame)
        assert report.filter(pl.col("column") == "position")["n_unique"][0] == 1
        assert report.filter(pl.col("column") == "player_id")["n_unique"][0] == 1
        # Numeric feature columns are not covered by cardinality.
        assert "feature_a" not in report["column"].to_list()


class TestMissingValueReport:
    def test_overall_null_fraction(self) -> None:
        frame = _matrix(10).with_columns(
            pl.when(pl.arange(0, 10) < 3)
            .then(None)
            .otherwise(pl.col("feature_c"))
            .alias("feature_c")
        )
        report = missing_value_report(frame)
        row = report.filter(pl.col("column") == "feature_c").row(0, named=True)
        assert row["null_count"] == 3
        assert row["null_fraction"] == pytest.approx(0.3)

    def test_masked_feature_broken_out_by_its_own_obs_column(self) -> None:
        frame = _matrix(10)
        # Half the rows have obs_defensive False and a null cbi feature;
        # the other half have obs_defensive True and a real value.
        frame = frame.with_columns(
            (pl.arange(0, 10) < 5).alias("obs_defensive"),
        ).with_columns(
            pl.when(pl.col("obs_defensive"))
            .then(pl.col("cbi_sum_last_3"))
            .otherwise(None)
            .alias("cbi_sum_last_3")
        )

        report = missing_value_report(frame)
        row = report.filter(pl.col("column") == "cbi_sum_last_3").row(0, named=True)

        assert row["obs_column"] == "obs_defensive"
        assert row["null_fraction_when_observed"] == pytest.approx(0.0)
        assert row["null_fraction_when_not_observed"] == pytest.approx(1.0)

    def test_unmasked_feature_has_no_obs_column(self) -> None:
        frame = _matrix(10)
        report = missing_value_report(frame)
        row = report.filter(pl.col("column") == "feature_a").row(0, named=True)
        assert row["obs_column"] is None


class TestVarianceReport:
    def test_constant_column_has_zero_variance_flagged_near_zero(self) -> None:
        frame = _matrix(10).with_columns(pl.lit(1.0).alias("feature_a"))
        report = variance_report(frame)
        row = report.filter(pl.col("column") == "feature_a").row(0, named=True)
        assert row["variance"] == 0.0
        assert row["near_zero_variance"] is True

    def test_normal_column_is_not_flagged(self) -> None:
        frame = _matrix(200)
        report = variance_report(frame)
        row = report.filter(pl.col("column") == "feature_c").row(0, named=True)
        assert row["near_zero_variance"] is False


class TestDistributionReport:
    def test_symmetric_distribution_has_near_zero_skew(self) -> None:
        frame = _matrix(2000)
        report = distribution_report(frame)
        row = report.filter(pl.col("column") == "feature_c").row(0, named=True)
        assert row["skewness"] == pytest.approx(0.0, abs=0.3)


class TestOutlierReport:
    def test_flags_an_injected_extreme_value(self) -> None:
        values = [0.0] * 99 + [1000.0]
        frame = _matrix(100).with_columns(pl.Series("feature_c", values))
        report = outlier_report(frame)
        row = report.filter(pl.col("column") == "feature_c").row(0, named=True)
        assert row["iqr_outlier_count"] >= 1
        assert row["zscore_outlier_count"] >= 1


class TestCorrelationMatrices:
    def test_perfectly_correlated_pair_has_r_close_to_one(self) -> None:
        frame = _matrix(200)
        pearson, _spearman = correlation_matrices(frame)
        r = pearson.filter(pl.col("column") == "feature_a")["feature_b"][0]
        assert r == pytest.approx(1.0, abs=1e-6)

    def test_independent_feature_has_low_correlation(self) -> None:
        frame = _matrix(500)
        pearson, _spearman = correlation_matrices(frame)
        r = pearson.filter(pl.col("column") == "feature_a")["feature_c"][0]
        assert abs(r) < 0.3


class TestHighCorrelationPairs:
    def test_flags_the_known_collinear_pair(self) -> None:
        frame = _matrix(200)
        pearson, _spearman = correlation_matrices(frame)
        pairs = high_correlation_pairs(pearson, threshold=0.9)
        flagged = {
            frozenset((row["feature_a"], row["feature_b"])) for row in pairs.iter_rows(named=True)
        }
        assert frozenset(("feature_a", "feature_b")) in flagged

    def test_does_not_flag_independent_feature(self) -> None:
        frame = _matrix(500)
        pearson, _spearman = correlation_matrices(frame)
        pairs = high_correlation_pairs(pearson, threshold=0.9)
        flagged = {
            frozenset((row["feature_a"], row["feature_b"])) for row in pairs.iter_rows(named=True)
        }
        assert frozenset(("feature_a", "feature_c")) not in flagged


class TestVifReport:
    def test_collinear_pair_has_high_vif(self) -> None:
        frame = _matrix(200)
        report = vif_report(frame, columns=["feature_a", "feature_b", "feature_c"])
        vif_a = report.filter(pl.col("column") == "feature_a")["vif"][0]
        assert vif_a > 100


class TestMutualInformationReport:
    def test_returns_one_row_per_feature_target_pair(self) -> None:
        frame = _matrix(200)
        report = mutual_information_report(
            frame, targets=["label_total_points_fpl"], columns=["feature_a", "feature_c"]
        )
        assert report.height == 2
        assert set(report["feature"].to_list()) == {"feature_a", "feature_c"}

    def test_predictive_feature_has_higher_mi_than_independent_one(self) -> None:
        frame = _matrix(500)
        report = mutual_information_report(
            frame, targets=["label_total_points_fpl"], columns=["feature_a", "feature_c"]
        )
        mi_a = report.filter(pl.col("feature") == "feature_a")["mutual_information"][0]
        mi_c = report.filter(pl.col("feature") == "feature_c")["mutual_information"][0]
        assert mi_a > mi_c


class TestTargetCorrelationReport:
    def test_predictive_feature_has_higher_correlation_than_independent_one(self) -> None:
        frame = _matrix(200)
        report = target_correlation_report(
            frame, targets=["label_total_points_fpl"], columns=["feature_a", "feature_c"]
        )
        row_a = report.filter(pl.col("feature") == "feature_a").row(0, named=True)
        row_c = report.filter(pl.col("feature") == "feature_c").row(0, named=True)
        assert abs(row_a["pearson_r"]) > abs(row_c["pearson_r"])

    def test_sorted_by_descending_absolute_pearson_within_target(self) -> None:
        frame = _matrix(200)
        report = target_correlation_report(
            frame, targets=["label_total_points_fpl"], columns=["feature_a", "feature_c"]
        )
        abs_r = report["pearson_r"].abs().to_list()
        assert abs_r == sorted(abs_r, reverse=True)


class TestRunEdaSweep:
    def test_bundles_every_report(self) -> None:
        frame = _matrix(200)
        result = run_eda_sweep(
            frame,
            vif_columns=["feature_a", "feature_b", "feature_c"],
            sample_size=200,
        )

        assert result.column_kinds.height == len(frame.columns)
        assert result.missing.height == len(frame.columns)
        assert result.variance.height == 4
        assert result.distribution.height == 4
        assert result.outliers.height == 4
        assert result.pearson.height == 4
        assert result.spearman.height == 4
        assert result.vif.height == 3
        assert result.mutual_information.height == 4 * len(LABEL_COLUMNS)
        assert result.target_correlation.height == 4 * len(LABEL_COLUMNS)
