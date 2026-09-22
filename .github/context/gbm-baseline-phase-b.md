# Phase B — LightGBM candidate model vs. the GLM baseline

Follows `.github/context/model-prototype-phase-9.md` §3 ("Phase B sketch"). This task scopes
down to just §3.2's first candidate-model slice: train a gradient-boosted-tree model per
(component, position), evaluate it with the existing Phase A evaluation harness on the
validation split, and report it side by side with the existing GLM baseline. Tuning (§3.3),
the walk-forward harness (§3.4), the model registry/artefact contract (§3.5), the final test-split
read (§3.6), and post-prediction aggregation (§3.7) are explicitly out of scope for this task.

## Section 1 — Q&A (verbatim)

**Q (scope).** Which piece of Phase B should we start with? Options offered: candidate model
comparison (train gradient-boosted trees and/or a hierarchical Bayesian Poisson model, compare
against the GLM baseline on validation using the existing evaluation.py metrics); walk-forward
backtest harness; model registry & artefact contract; tuning strategy (optuna); other.

**A.** `candidate_models`. Production code, full TDD (not prototyping).

**Q (library).** Which gradient-boosted-tree library? LightGBM / XGBoost / both trained and
compared side by side.

**A.** LightGBM.

**Q (feature set).** Same primary features as the GLM baseline (excludes era-masked rolling
columns), or all numeric features including era-masked ones (`obs_defensive`/`obs_bps_inputs`/
`obs_expected`/`obs_starts` groups) that the GLM baseline excludes, since trees handle NaN
natively and the Phase B sketch (§3.2) specifically calls this out as a reason to try trees?

**A.** All numeric features, including the era-masked ones.

**Q (position handling).** Position-specific models mirroring the GLM baseline exactly (one
fitted model per (component, position)), or one model per component with position as a
feature (one-hot/native categorical)?

**A.** Position-specific models, mirroring the GLM baseline exactly (one fitted model per
(component, position)).

**Q (components).** Which targets does the new model predict? Same as `GLM_COMPONENTS`
(`minutes` as stage 1, then `goals_scored`/`assists`/`goals_conceded`/`bonus`/
`defensive_contribution` as stage 2), leaving `saves`/cards/penalties/`own_goals` naive-only —
or something narrower/wider?

**A.** Same as `GLM_COMPONENTS` exactly, as described above.

**Q (hyperparameters).** Tune now, or fixed defaults with tuning deferred to a later task?

**A.** Fixed, reasonable defaults for this task; tuning (optuna) is a separate later Phase B task.

**Q (CLI/report).** New CLI command and new report file, or extend the existing `fpl baseline`
command?

**A.** New CLI command (e.g. `fpl gbm-baseline`) producing a new `docs/model-prototype-gbm.md`
report, following the same "regenerated from scratch, do not hand-edit" convention as
`docs/model-prototype-baseline.md`.

## Section 2 — Implementation plan

Conventions binding every step: full TDD (failing test first, then implementation), matches
existing repo style (polars, dataclasses, `from __future__ import annotations`), never touches
the test split, `ruff check`/`ruff format` clean before each commit, one commit per step, push
and verify CI green (`gh run watch`) before moving to the next step.

1. **Add the `lightgbm` dependency.** Edit `pyproject.toml` to add `lightgbm` to
   `[project].dependencies`. Run `uv sync` and confirm it installs cleanly on this Windows
   machine (LightGBM ships prebuilt wheels for Windows/py3.12, so no compiler toolchain should
   be needed — verify this is actually true before proceeding). Run `uv run python -c "import
   lightgbm"` to confirm importability.

2. **Failing tests for the new module.** Add `tests/training/test_gbm_baseline.py`, structured
   like `tests/training/test_baseline.py`, covering: fitting one LightGBM minutes model per
   position and one LightGBM component model per (component, position) on a small synthetic
   training frame; predictions returned as `lgbm_<target>` columns; a position/component with no
   training rows has no fitted model and predicts `None`/`NaN` (mirroring
   `predict_glm_baseline`'s contract); the feature set actually used includes at least one
   era-masked rolling column (proving parity with the GLM baseline's exclusion is *not*
   inherited). Confirm these tests fail (module doesn't exist yet).

3. **Implement `src/fpl/training/gbm_baseline.py`.** Mirror `baseline.py`'s `GlmBaseline`/
   `fit_glm_baseline`/`predict_glm_baseline` shape as `GbmBaseline`/`fit_gbm_baseline`/
   `predict_gbm_baseline`: same two-stage design (minutes stage 1 per position via
   `lightgbm.LGBMRegressor(objective="regression")`; each of `GLM_COMPONENTS` stage 2 per
   (component, position) via `lightgbm.LGBMRegressor(objective="poisson")`, trained only on
   `label_minutes > 0` rows, same `P(play) x E[component|play]` combination rule as the GLM
   baseline for consistency). Feature columns come from `eda.numeric_feature_columns(frame)`
   (the full set, including era-masked columns) rather than `baseline.primary_feature_columns`,
   with the same "drop all-null columns" guard `fit_glm_baseline` already has. No imputer/scaler
   step — LightGBM handles missing values and unscaled features natively. Fixed, reasonable
   defaults for every hyperparameter (e.g. `n_estimators`, `max_depth`, `learning_rate`,
   `min_child_samples`) chosen conservatively for this dataset's size, documented in a module
   docstring comment on *why* each default was chosen. Run the new tests from step 2 and confirm
   they pass.

4. **Generalize `assemble_predicted_points` for reuse.** Edit
   `src/fpl/training/evaluation.py` so `assemble_predicted_points` takes a `model_prefix: str =
   "glm"` parameter (used to build the `f"{model_prefix}_{component}"` required-column names
   instead of the hardcoded `"glm_"` prefix), preserving its exact current behaviour when called
   with no prefix argument. Update `tests/training/test_evaluation.py` with a new test calling it
   with `model_prefix="lgbm"` against `lgbm_`-prefixed columns, while leaving every existing
   `glm_`-prefixed test unchanged and passing.

5. **New report renderer `src/fpl/training/gbm_report.py`.** Mirror
   `baseline_report.py`'s structure: split summary, per-component/per-position regression metrics
   for the LightGBM model, system score (`assemble_predicted_points(..., model_prefix="lgbm")` +
   `points_error_report`), and `spearman_by_gameweek` — plus the equivalent GLM numbers
   recomputed in the same run, laid out as a side-by-side comparison (GLM column vs. LightGBM
   column) so the report directly answers "did trees improve on the baseline". Add
   `tests/training/test_gbm_report.py` mirroring `test_baseline_report.py`'s test shape (small
   synthetic frame, assert the rendered markdown contains expected section headers/values).

6. **Wire up `fpl gbm-baseline` in `src/fpl/cli.py`.** New Typer subcommand with the same
   argument surface as the existing `fpl baseline` command (seasons, `--data-root`, output path
   default `docs/model-prototype-gbm.md`): builds the training matrix, fits both GLM and
   LightGBM baselines, evaluates both on the validation split, renders the report from step 5,
   writes it to disk. Add a CLI test in `tests/test_cli.py` mirroring the existing `fpl baseline`
   CLI test (small fixture data, assert exit code 0 and the report file is written).

7. **Run the full test suite and lint.** `uv run pytest` and `uv run ruff check .` / `uv run ruff
   format --check .` clean. Fix anything red before proceeding.

8. **Generate the real report.** Run `uv run fpl gbm-baseline` against the real on-disk 10-season
   data (same seasons/split as `docs/model-prototype-baseline.md`) to produce a real
   `docs/model-prototype-gbm.md` — the one non-TDD, "run on real data" step, matching how the
   GLM baseline report was originally produced (Phase A Step 32).

9. **Docs closeout note.** Add a short dated note to `.github/context/model-prototype-phase-9.md`
   §3.2 (mirroring the existing "2026-08-24 update" / "closed out" convention already used for
   §3.1) recording that a first LightGBM candidate has been trained and compared against the GLM
   baseline, with a one-line pointer to the resulting numbers in `docs/model-prototype-gbm.md`
   and an explicit note that tuning/walk-forward/registry/final-test-read remain open for later
   Phase B tasks.

10. **Commit and verify.** One commit per step above (per the user's established workflow:
    commit, push, `gh run watch` to confirm CI green, before starting the next step).
