# Subsystem 3 close-out + MVP optimiser + MVP Pages site

Follows `.github/context/gbm-baseline-phase-b.md`. That task compared LightGBM against the GLM
baseline and left the model registry/artefact contract (spec §3.5), the walk-forward harness
(§3.4), tuning (§3.3) and post-prediction aggregation (§3.7) explicitly open. This task closes out
just enough of subsystem 3 to unblock a first end-to-end vertical slice — registry, inference,
a heuristic optimiser (subsystem 4 MVP), and a static Pages site (subsystem 5 MVP) — deferring
tuning, the full walk-forward harness, and multi-period/chip strategy to later tasks.

## Section 1 — Q&A (verbatim)

**Q (which model to freeze).** GLM or LightGBM?

**A.** GLM — it beats the LightGBM candidate on the system-level metrics that matter for player
selection (overall predicted-points MAE 1.23 vs 1.37; mean gameweek Spearman 0.65 vs 0.61,
`docs/model-prototype-gbm.md`). LightGBM's feature set also includes era-masked rolling columns
that `features.library.build` (the inference entrypoint) does not currently populate, which would
need extra work GLM does not.

**Q (scope for this work session).** Registry/inference only, or continue straight into the
heuristic optimiser and a minimal Pages site in the same session?

**A.** `full_slice` — continue through the optimiser and a minimal site.

**Q (production training data).** Keep the existing train split only (2016-17..2023-24), or refit
on train+validation (2016-17..2024-25) for the frozen production artefact?

**A.** Refit on 2016-17..2024-25. The 2025-26 test season stays untouched (spec §3.6 — touched
exactly once, later). Additionally score the frozen model against whatever games have already
been played in the live 2026-27 season (5 gameweeks in at the time of writing) as a freshness
check — genuinely new data, unseen by training or by the existing validation/test splits.

**Q (`models/active.json` granularity).** One pointer for the whole GLM bundle, or one pointer per
component (minutes/goals_scored/assists/goals_conceded/bonus/defensive_contribution)?

**A.** Per-component. Everything points at GLM today, but a future model swap for e.g. just
`bonus` becomes a one-line diff instead of a rewrite.

**Q (CLI command name for inference).** 

**A.** `fpl predict`.

**Q (optimiser algorithm).** MILP solver (matches spec's cited prior art, sertalpbilal's
FPL-Optimization-Tools) or a simpler heuristic for this first slice?

**A.** Heuristic for now (greedy squad construction respecting budget/formation/club-limit
constraints) — explicitly not claimed optimal. A real MILP solver is deferred to a proper
subsystem 4 task; this one only needs to produce a *valid, reasonable* pick end-to-end so the
site has something real to show.

**Q (squad rules used).** Not documented anywhere in this repo's `docs/*.md` — confirmed from
general FPL knowledge, flagged here for review:
- 15-player squad: 2 GK, 5 DEF, 5 MID, 3 FWD (`docs/Fantasy Premier League Rules.md` §Squad Size).
- £100m total budget (same doc, §Budget).
- Starting XI: 1 GK, at least 3 DEF, at least 1 FWD, 11 total (same doc, §Managing Your Squad).
- **Max 3 players from any one Premier League club** — standard FPL rule, not stated in this
  repo's docs; used here on general knowledge and should be corrected if wrong.

**Q (site scope).** Full interactive squad builder, or a static page rendering one precomputed
recommendation?

**A.** (Not asked verbatim — inferred from "get an ugly end-to-end site live" framing in prior
conversation.) A single static page reading a committed JSON artefact: recommended 15-man squad,
starting XI, captain/vice-captain, bench order, each with predicted points. No client-side
solver, no personal-squad import — those are later subsystem 5 iterations.

## Section 2 — Implementation plan

Conventions binding every step (matching `gbm-baseline-phase-b.md`): full TDD (failing test first),
existing repo style (polars, dataclasses, `from __future__ import annotations`), never touches the
2025-26 test split, `ruff check`/`ruff format` clean before each commit, one commit per step, push
and verify CI green (`gh run watch`) before the next step.

### Phase C — Registry, inference, predictions archive

1. Add `joblib` dependency (needed to persist sklearn `Pipeline` objects); `uv sync`; confirm import.
2. `src/fpl/storage/paths.py`: add `model_artefact_dir(component, model_name, version, *, data_root)`
   → `models/<component>/<model_name>-<version>/`, and `predictions_partition(season, as_of, *,
   data_root)` → `data/predictions/season=.../as_of=.../`, mirroring existing helpers' style
   (`_check_component` validation, docstring explaining the convention). Tests in
   `tests/storage/test_paths.py`.
3. `src/fpl/training/registry.py`: `save_component_artefact(...)` (writes `model.joblib` +
   `metadata.json` — ordered feature list, feature-registry hash, scoring-rules version, training
   `as_of`/season range, evaluation metrics, git SHA) and `write_active_pointer(...)` /
   `load_active_bundle(data_root=...)` (reads `models/active.json`, loads each component's
   artefact, reassembles a `fpl.training.baseline.GlmBaseline`-shaped bundle so the existing
   `predict_glm_baseline` can be reused unchanged rather than duplicating prediction logic). TDD
   with a temp `data_root`.
4. `src/fpl/inference/__init__.py` + `src/fpl/inference/predict.py`: `predict_next_gameweek(season,
   as_of, *, horizon_gameweeks=1, data_root=None)` — calls `features.library.build`, asserts the
   frame has every required feature column per loaded artefact (hard fail, not warning, per spec
   §3.5), reuses `predict_glm_baseline` + a new small naive-component helper (below) +
   `assemble_predicted_points`, returns one row per (player, fixture) with every predicted
   component and `predicted_total_points_fpl`.
5. `src/fpl/inference/naive.py`: trailing-mean prediction for the naive-only components
   (`saves`/`yellow_cards`/`red_cards`/`penalties_saved`/`penalties_missed`/`own_goals`) computed
   directly from `facts/player_fixture` history before `as_of` — a horizon-call-shaped adaptation
   of `training.baseline.naive_rolling_mean_predictions`'s trailing-window logic, since that
   function expects a fully assembled multi-event training frame rather than a single future
   horizon row per player.
6. `fpl train-glm --seasons ... --version ...` CLI command: builds the training matrix for the
   given seasons (default 2016-17..2024-25), fits the GLM bundle, evaluates it against whatever
   2026-27 gameweeks are already in `facts/player_fixture` (a genuine freshness check, reported not
   gated), writes each component's artefact + updates `models/active.json`.
7. `fpl predict --season ... --as-of ...` CLI command (default `as_of` = now, default season =
   `CURRENT_SEASON`): runs `predict_next_gameweek`, archives the result to
   `data/predictions/season=.../as_of=.../part.parquet`.
8. Run the full test suite + lint clean. Run `fpl train-glm` for real (2016-17..2024-25) to produce
   the first committed artefact set, then `fpl predict` for the next unplayed 2026-27 gameweek to
   produce the first real predictions archive.

   **Outcome (2026-09-22):** `train-glm` ran for real — 223,762 rows, 9 seasons, `models/`
   committed. `predict` did not produce a usable archive: the live 2026-27 season is 5 gameweeks
   in, but ingestion has never staged its per-player match stats (`daily-snapshot.yml` only pulls
   `bootstrap-static`/`fixtures`/`entry`; `player_fixture_stats` is built solely from vaastav's
   historical archive, which doesn't cover the live season), so every `naive_*` component has zero
   within-season history and every row comes back null. A past season can't stand in either —
   `staged/players` is only ever kept for the *current* season. Decided with the user: document the
   gap here and in `model-prototype-phase-9.md` §3.5 as follow-up (closing the live-ingestion gap is
   subsystem-2 scope), commit the real `models/` artefacts as-is, and proceed to Phase D/E — the
   registry/inference code path is already proven by `tests/inference/` + the new CLI tests. No
   predictions archive is committed this session.
9. Docs closeout note in `.github/context/model-prototype-phase-9.md` §3.5 recording the registry
   contract has landed (narrower than the full spec — no walk-forward harness, no tuning yet).

### Phase D — MVP heuristic optimiser (subsystem 4, first slice)

10. `src/fpl/optimiser/rules.py`: squad-construction constants (2 GK/5 DEF/5 MID/3 FWD, £100m,
    max 3/club) as named constants with a docstring citing this doc's Q&A for the max-3/club rule.
11. `src/fpl/optimiser/squad.py`: `pick_squad(predictions_frame, *, budget=100.0) ->
    SquadSelection` — greedy heuristic (fill each position's quota by predicted-points-per-price
    ratio subject to remaining budget and per-club count, backtrack/swap if the budget can't close
    exactly), explicitly documented as a heuristic, not a global optimum. `pick_starting_xi(squad)
    -> (xi, bench_ordered, captain, vice_captain)` — best-scoring valid formation, captain = top
    predicted scorer, vice = second. Full TDD against small synthetic prediction frames covering
    the constraint edges (club limit binds, budget binds, formation minimums).
12. `fpl optimise --season ... --as-of ...` CLI command: reads the latest predictions archive
    partition, runs the squad/XI pick, writes `data/predictions/season=.../as_of=.../squad.json`
    (or a sibling file) with the full recommendation.
13. Full test suite + lint clean; run once for real to produce the first real recommendation.

    **Outcome (2026-09-22):** no real recommendation is produced this session. `fpl optimise`
    needs a non-null predictions archive, and Phase C step 8 already found that `fpl predict`
    cannot currently produce one — the live 2026-27 season has no staged per-player match stats
    (same live-ingestion gap), so every predicted-points column comes back null and `pick_squad`
    would have nothing to rank. Rather than repeat that investigation, this is recorded up front:
    `fpl optimise`'s own test suite (`tests/test_cli.py::TestOptimiseCommand`) proves the command
    works correctly against a valid predictions frame; a real end-to-end run is blocked on the
    same subsystem-2 follow-up (staging FPL's `event/{event}/live/` endpoint) already logged in
    `model-prototype-phase-9.md` §3.5 and step 8 above.

### Phase E — MVP static Pages site (subsystem 5, first slice)

14. `site/index.html` + `site/style.css` + `site/app.js`: fetches the committed recommendation
    JSON, renders squad/starting-XI/bench/captain with predicted points — plain HTML/CSS/JS, no
    build step, no framework (spec: "nothing server-side runs at request time").
15. `.github/workflows/publish-site.yml`: on push to `master` (paths: `site/**`,
    `data/predictions/**`) and `workflow_dispatch`, deploy `site/` to GitHub Pages via
    `actions/deploy-pages`.
16. `.github/workflows/weekly-predict.yml`: scheduled shortly after each gameweek deadline —
    `fpl predict` then `fpl optimise`, commit the new predictions/recommendation, which in turn
    triggers step 15's publish workflow.
17. Manually verify the deployed Pages URL renders the real recommendation end-to-end.

    **Outcome (2026-09-23):** both workflows are live and verified green (`gh run watch`).
    `publish-site.yml` deploys successfully; the live URL serves `index.html`/`app.js`/`style.css`
    (200) and `data/predictions/latest.json` correctly 404s (nothing archived yet), which `app.js`
    turns into its "not published yet" fallback — the same path already exercised by the jsdom
    smoke test in step 14. `weekly-predict.yml`'s first real run surfaced a genuine bug: `predict`
    can produce a non-empty, entirely-null frame (the live-ingestion gap, steps 8/13) that the old
    row-count check accepted and archived — fixed by treating an all-null result the same as
    "nothing to predict" (commit `ee795b1`), plus a workflow guard for the case where
    `data/predictions/` doesn't exist yet at all (commit `d82621b`, since git doesn't track empty
    directories). A second manual re-run confirmed a fully clean skip: predict skips, optimise is
    correctly not attempted, nothing is committed. No real recommendation exists yet — end-to-end
    "renders the real recommendation" is still blocked on the live-ingestion gap, not on anything
    in this phase.

    **Second outcome (2026-09-23, after the live-ingestion gap closed):** with real, non-null
    predictions flowing, `weekly-predict.yml` committed a genuine recommendation for the first
    time — but the site still showed "not published yet", exposing a second real bug in "which in
    turn triggers step 15's publish workflow" above: it doesn't. GitHub suppresses a workflow's
    `push` trigger specifically for commits made by *another* workflow using the default
    `GITHUB_TOKEN` (anti-recursion protection), so `weekly-predict.yml`'s own `git push` can never
    fire `publish-site.yml`'s `push: paths: data/predictions/**` trigger — confirmed by history:
    no `publish-site.yml` run had ever been triggered by a bot commit, only by human pushes and
    manual `workflow_dispatch`. Fixed by adding a `workflow_run: workflows: ["Weekly predict and
    optimise"], types: [completed]` trigger to `publish-site.yml`, which reacts to that workflow's
    run *completing* rather than to the commit it made, sidestepping the restriction entirely.
    Verified: manually triggering `weekly-predict.yml` produced a real squad recommendation;
    manually triggering `publish-site.yml` (standing in for the new automatic trigger, since it
    wasn't live for that specific run yet) deployed it, and the live URL now serves a real
    `latest.json` pointing at a genuine `squad.json`. The next scheduled `weekly-predict.yml` run
    will be the first to confirm the new `workflow_run` trigger fires automatically end-to-end.

18. Follow-up (2026-09-23): the site rendered players/teams as raw ids (`Player #572 · Team 11`)
    since neither name was in the `squad.json` artefact, tracked as a follow-up in `app.js`'s
    original comment. Closed by enriching `squad.json` at `fpl optimise` time — the CLI now reads
    the season's already-staged `players`/`teams` tables (`web_name`/`short_name`, the same short
    display names the real FPL app uses) and joins them onto each player entry as `name`/
    `team_name`, via two new helpers in `cli.py` (`_name_lookups`, extended `_player_payload`). The
    optimiser core (`SquadPlayer`, `pick_squad`, `pick_starting_xi`) is untouched — this is purely
    a display concern at the CLI/payload layer. Missing staged tables fall back to an id-based
    label (`Player #{id}`/`Team {id}`) rather than failing, so every existing test/partition still
    works unmodified. `site/app.js`'s `playerLabel()` now renders `"Salah (LIV)"` instead of the
    id pair.

19. Follow-up (2026-09-23): a user noted the published recommendation left ~£28.7m of the £100m
    budget unspent (£71.3m squad value, 45.9 predicted pts), which is never correct given there is
    no reward for unspent budget — a symptom of `pick_squad`'s greedy heuristic (best
    points-per-price first, subject to affordability), which this task's Q&A above explicitly
    flagged as "not claimed optimal" and deferred to "a proper subsystem 4 task". That task starts
    now: `pick_squad` was rewritten from the greedy fill to an *exact* 0/1 knapsack-style MILP
    (`scipy.optimize.milp`, HiGHS backend, already a transitive dependency via `scipy>=1.18` — no
    new dependency needed) that maximises total predicted points subject to budget, the four
    position quotas (as equality constraints) and the per-club cap, in one solve. A pre-flight
    per-position candidate count check still raises a specific `ValueError` (e.g. naming "GK")
    when a position can't be filled at all, rather than surfacing scipy's generic infeasible
    status. `pick_starting_xi` is unchanged (already exact). One existing test
    (`test_double_gameweek_points_are_summed_before_ranking`) encoded the old greedy's "squeeze
    out the pricier player" behaviour as correct; it was rewritten to assert the true optimum
    (both players kept, +18 total FWD points instead of +11), and a new
    `test_spends_the_full_budget_when_it_raises_total_points` test was added. Verified against
    real staged 2026-27 data: the same partition that used to yield £71.3m/45.9 pts now yields
    £98.9m/48.9 pts.

### Explicitly out of scope for this task (carried forward)

Tuning (§3.3), the full walk-forward backtest harness (§3.4), transfers/chip-timing strategy,
multi-gameweek horizon planning, personal-squad import, and post-prediction double/blank-gameweek
aggregation (§3.7).
