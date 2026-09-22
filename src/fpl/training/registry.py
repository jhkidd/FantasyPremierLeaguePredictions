"""Model registry: per-component artefact persistence and the
``models/active.json`` pointer (spec §3.5,
`.github/context/subsystem3-close-and-mvp-site.md` Phase C step 3).

The registry's granularity is per-component (minutes, plus each of
:data:`~fpl.training.baseline.GLM_COMPONENTS`) rather than one pointer for
the whole bundle — everything points at GLM today, but a future model swap
for just one component (e.g. a specialised ``bonus`` model) becomes a
one-line ``active.json`` diff instead of a rewrite.

:func:`load_active_bundle` reassembles a
:class:`~fpl.training.baseline.GlmBaseline`-shaped bundle from whatever
per-component artefacts ``active.json`` currently points at, so
:func:`~fpl.training.baseline.predict_glm_baseline` is reused unchanged at
inference rather than duplicating prediction logic — every component
artefact today happens to be a GLM pipeline fit from the same
``fit_glm_baseline`` call, so this is a lossless round trip, but the shape
generalises to a future non-GLM component artefact as long as it is stored
as a ``{position: predictor}`` mapping with a compatible ``.predict()``.

Every artefact's ``metadata.json`` records the exact ordered feature list
used to fit it. :func:`load_active_bundle` asserts every loaded component's
feature list is identical — a real divergence would mean two components
were fit against incompatible feature sets, which
:func:`~fpl.training.baseline.predict_glm_baseline` has no way to detect on
its own since it reads a single shared ``feature_columns`` list. This is a
hard fail, never a warning, per spec §3.5.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib

from fpl.scoring.base import Position
from fpl.storage import paths
from fpl.training.baseline import GLM_COMPONENTS, MINUTES_TARGET, GlmBaseline

__all__ = [
    "COMPONENT_NAMES",
    "load_active_bundle",
    "save_component_artefact",
    "write_active_pointer",
]

# The registry's full set of predictable targets: the minutes stage plus
# every GLM_COMPONENTS name. Order matches GlmBaseline's own two-stage
# design (minutes first).
COMPONENT_NAMES: tuple[str, ...] = (MINUTES_TARGET, *GLM_COMPONENTS)


def _git_sha() -> str | None:
    """The current commit, or ``None`` if unavailable (e.g. a source
    checkout with no ``.git`` directory) — never fatal, since a missing SHA
    degrades traceability rather than correctness."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def save_component_artefact(
    component: str,
    model_name: str,
    version: str,
    models: Mapping[Position, Any],
    *,
    feature_columns: Sequence[str],
    extra_metadata: Mapping[str, Any] | None = None,
    models_root: Path | None = None,
) -> Path:
    """Persist one component's ``{position: fitted predictor}`` mapping and
    its ``metadata.json`` to ``models/<component>/<model_name>-<version>/``.

    ``metadata.json`` records the ordered ``feature_columns`` list, the
    component/model/version identifiers, the producing git SHA, a creation
    timestamp, and anything in ``extra_metadata`` (e.g. training season
    range, evaluation metrics — spec §3.5's "training as_of range,
    evaluation metrics" fields, left to the caller since only it knows
    them).
    """
    directory = paths.model_artefact_dir(component, model_name, version, models_root=models_root)
    directory.mkdir(parents=True, exist_ok=True)
    joblib.dump(dict(models), directory / "model.joblib")

    metadata: dict[str, Any] = {
        "component": component,
        "model_name": model_name,
        "version": version,
        "feature_columns": list(feature_columns),
        "git_sha": _git_sha(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    (directory / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))

    return directory


def write_active_pointer(
    mapping: Mapping[str, Mapping[str, str]], *, models_root: Path | None = None
) -> Path:
    """Write ``models/active.json``: ``{component: {"model": ..., "version": ...}}``.

    Swapping which artefact serves a component is exactly this file's diff
    — the whole point of the per-component registry (module docstring).
    """
    path = paths.models_active_pointer(models_root=models_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=2, sort_keys=True))
    return path


def load_active_bundle(
    *, required_components: Sequence[str] = COMPONENT_NAMES, models_root: Path | None = None
) -> GlmBaseline:
    """Load every component in ``required_components`` per
    ``models/active.json`` and reassemble a :class:`GlmBaseline`-shaped
    bundle (module docstring).

    Raises ``FileNotFoundError`` if ``active.json`` itself does not exist,
    and ``ValueError`` if a required component is missing from it, or if
    two loaded components' recorded ``feature_columns`` disagree — both
    hard failures rather than silently predicting with a partial or
    mismatched bundle (spec §3.5).
    """
    pointer_path = paths.models_active_pointer(models_root=models_root)
    if not pointer_path.exists():
        raise FileNotFoundError(f"models/active.json not found: {pointer_path}")
    active = json.loads(pointer_path.read_text())

    missing = [component for component in required_components if component not in active]
    if missing:
        raise ValueError(f"models/active.json is missing required component(s): {missing}")

    feature_columns: list[str] | None = None
    minutes_models: dict[Position, Any] = {}
    component_models: dict[tuple[str, Position], Any] = {}

    for component in required_components:
        reference = active[component]
        directory = paths.model_artefact_dir(
            component, reference["model"], reference["version"], models_root=models_root
        )
        metadata = json.loads((directory / "metadata.json").read_text())
        component_feature_columns = metadata["feature_columns"]
        if feature_columns is None:
            feature_columns = component_feature_columns
        elif feature_columns != component_feature_columns:
            raise ValueError(
                f"feature schema mismatch for component {component!r}: its artefact's "
                f"feature_columns differ from an earlier-loaded component's - "
                "models/active.json points at incompatible artefacts"
            )

        models_for_component = joblib.load(directory / "model.joblib")
        if component == MINUTES_TARGET:
            minutes_models.update(models_for_component)
        else:
            for position, predictor in models_for_component.items():
                component_models[(component, position)] = predictor

    return GlmBaseline(
        feature_columns=feature_columns or [],
        excluded_masked_columns=[],
        minutes_models=minutes_models,
        component_models=component_models,
    )
