"""Tests for :mod:`fpl.training.registry` — per-component artefact
persistence and the ``models/active.json`` pointer (spec §3.5,
`.github/context/subsystem3-close-and-mvp-site.md` Phase C step 3)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from fpl.scoring.base import POSITIONS
from fpl.storage import paths
from fpl.training.baseline import GLM_COMPONENTS, MINUTES_TARGET, fit_glm_baseline
from fpl.training.registry import (
    COMPONENT_NAMES,
    load_active_bundle,
    save_component_artefact,
    write_active_pointer,
)


def _glm_frame(*, seed: int = 0, n_per_position: int = 40) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    for position in sorted(POSITIONS):
        for _ in range(n_per_position):
            feature_a = rng.normal()
            feature_b = rng.normal()
            played = rng.random() > 0.2
            minutes = float(rng.integers(60, 90)) if played else 0.0
            rows.append(
                {
                    "position": position,
                    "feature_a": feature_a,
                    "feature_b": feature_b,
                    "cbi_sum_last_3": feature_a,
                    "obs_defensive": True,
                    "obs_bps_inputs": True,
                    "obs_expected": True,
                    "obs_starts": True,
                    "label_minutes": minutes,
                    "label_goals_scored": float(rng.poisson(0.3)) if played else 0.0,
                    "label_assists": float(rng.poisson(0.2)) if played else 0.0,
                    "label_goals_conceded": float(rng.poisson(1.0)) if played else 0.0,
                    "label_bonus": float(rng.poisson(0.5)) if played else 0.0,
                    "label_defensive_contribution": float(rng.poisson(2.0)) if played else 0.0,
                }
            )
    return pl.DataFrame(rows)


def _save_all_components(bundle, *, version: str, models_root: Path) -> None:
    write_active_pointer(
        {component: {"model": "glm", "version": version} for component in COMPONENT_NAMES},
        models_root=models_root,
    )
    save_component_artefact(
        MINUTES_TARGET,
        "glm",
        version,
        bundle.minutes_models,
        feature_columns=bundle.feature_columns,
        models_root=models_root,
    )
    for component in GLM_COMPONENTS:
        models_for_component = {
            position: pipeline
            for (comp, position), pipeline in bundle.component_models.items()
            if comp == component
        }
        save_component_artefact(
            component,
            "glm",
            version,
            models_for_component,
            feature_columns=bundle.feature_columns,
            models_root=models_root,
        )


class TestSaveComponentArtefact:
    def test_writes_model_and_metadata_files(self, isolated_models_root: Path) -> None:
        frame = _glm_frame()
        bundle = fit_glm_baseline(frame)

        directory = save_component_artefact(
            MINUTES_TARGET,
            "glm",
            "v1",
            bundle.minutes_models,
            feature_columns=bundle.feature_columns,
        )

        assert directory == paths.model_artefact_dir(MINUTES_TARGET, "glm", "v1")
        assert (directory / "model.joblib").exists()
        assert (directory / "metadata.json").exists()

    def test_metadata_records_ordered_feature_list(self, isolated_models_root: Path) -> None:
        frame = _glm_frame()
        bundle = fit_glm_baseline(frame)

        directory = save_component_artefact(
            MINUTES_TARGET,
            "glm",
            "v1",
            bundle.minutes_models,
            feature_columns=bundle.feature_columns,
        )

        metadata = json.loads((directory / "metadata.json").read_text())
        assert metadata["feature_columns"] == bundle.feature_columns
        assert metadata["component"] == MINUTES_TARGET
        assert metadata["model_name"] == "glm"
        assert metadata["version"] == "v1"

    def test_extra_metadata_is_merged_in(self, isolated_models_root: Path) -> None:
        frame = _glm_frame()
        bundle = fit_glm_baseline(frame)

        directory = save_component_artefact(
            MINUTES_TARGET,
            "glm",
            "v1",
            bundle.minutes_models,
            feature_columns=bundle.feature_columns,
            extra_metadata={"training_seasons": ["2016-17", "2017-18"]},
        )

        metadata = json.loads((directory / "metadata.json").read_text())
        assert metadata["training_seasons"] == ["2016-17", "2017-18"]


class TestActivePointerRoundTrip:
    def test_load_active_bundle_reproduces_identical_predictions(
        self, isolated_models_root: Path
    ) -> None:
        from fpl.training.baseline import predict_glm_baseline

        frame = _glm_frame()
        bundle = fit_glm_baseline(frame)
        _save_all_components(bundle, version="v1", models_root=isolated_models_root)

        loaded = load_active_bundle()

        original_predictions = predict_glm_baseline(bundle, frame)
        loaded_predictions = predict_glm_baseline(loaded, frame)
        for component in (*GLM_COMPONENTS, MINUTES_TARGET):
            column = f"glm_{component}"
            assert loaded_predictions[column].to_list() == pytest.approx(
                original_predictions[column].to_list(), nan_ok=True
            )

    def test_missing_active_pointer_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="active.json"):
            load_active_bundle()

    def test_missing_required_component_raises(self, isolated_models_root: Path) -> None:
        write_active_pointer(
            {MINUTES_TARGET: {"model": "glm", "version": "v1"}},
            models_root=isolated_models_root,
        )
        frame = _glm_frame()
        bundle = fit_glm_baseline(frame)
        save_component_artefact(
            MINUTES_TARGET,
            "glm",
            "v1",
            bundle.minutes_models,
            feature_columns=bundle.feature_columns,
        )

        with pytest.raises(ValueError, match="goals_scored"):
            load_active_bundle()

    def test_feature_schema_mismatch_across_components_raises(
        self, isolated_models_root: Path
    ) -> None:
        frame = _glm_frame()
        bundle = fit_glm_baseline(frame)
        _save_all_components(bundle, version="v1", models_root=isolated_models_root)

        # Corrupt one component's recorded feature list after the fact.
        directory = paths.model_artefact_dir("bonus", "glm", "v1")
        metadata_path = directory / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["feature_columns"] = [*metadata["feature_columns"], "an_extra_column"]
        metadata_path.write_text(json.dumps(metadata))

        with pytest.raises(ValueError, match="feature schema mismatch"):
            load_active_bundle()
