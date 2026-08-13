# Model prototype — Phase 0 (data correctness) + Phase A (EDA & baseline)

Status: **planned** — not yet implemented.

Supersedes nothing. Follows `.github/context/features-library-phase-8.md` (Phase 8, committed as
`e871d34`).

---

## Section 0 — Empirical findings that shaped this plan

These were established by direct inspection of the repository and the committed `data/` tree during
requirements gathering. They are recorded here because several of them contradict what the code
currently assumes, and the plan below depends on them.

### 0.1 `facts/player_fixture` is complete; the *entrypoint* is not

`data/facts/player_fixture/` holds **253,509 rows across all ten seasons** (2016-17 → 2025-26), with
every target column and every `obs_*` availability mask intact.

| Season | Rows | Cumulative | Cum. % |
|---|---:|---:|---:|
| 2016-17 | 23,679 | 23,679 | 9.3% |
| 2017-18 | 22,467 | 46,146 | 18.2% |
| 2018-19 | 21,790 | 67,936 | 26.8% |
| 2019-20 | 22,501 | 90,437 | 35.7% |
| 2020-21 | 24,365 | 114,802 | 45.3% |
| 2021-22 | 25,447 | 140,249 | 55.3% |
| 2022-23 | 26,505 | 166,754 | 65.8% |
| 2023-24 | 29,725 | 196,479 | 77.5% |
| 2024-25 | 27,283 | 223,762 | 88.3% |
| 2025-26 | 29,747 | 253,509 | 100.0% |

However `features.library.build()` **cannot produce a training set for any historical season**. It is
wired for forward-looking inference and requires four inputs:

| Input | Purpose | Coverage |
|---|---|---|
| `staged/fixtures` | the upcoming-fixture list | **2026-27 only** |
| `staged/players` | current price, team-of-record fallback | **2026-27 only** |
| `facts/player_fixture` | history → rolling features | ✅ 10 seasons |
| `facts/team_fixture` | elo, odds, congestion | **2026-27 only** |

`staged/fixtures` and `staged/players` come from the FPL API bootstrap endpoint, which only ever
serves the *current* season. Re-running ingest cannot fix this.

Training does not need the upcoming-fixture list — we already know which fixtures happened, because
they are rows in `facts/player_fixture`. Hence the facts-native dataset builder in Phase A.

### 0.2 Availability tiers, confirmed empirically

`obs_*` mask counts per season confirm §4 of the design spec exactly:

| Season | `obs_defensive` | `obs_bps_inputs` | `obs_expected` | `obs_starts` |
|---|---:|---:|---:|---:|
| 2016-17 | 23,679 | 23,679 | 0 | 0 |
| 2017-18 | 22,467 | 22,467 | 0 | 0 |
| 2018-19 | 21,790 | 21,790 | 0 | 0 |
| 2019-20 | 0 | 0 | 0 | 0 |
| 2020-21 | 0 | 0 | 0 | 0 |
| 2021-22 | 0 | 0 | 0 | 0 |
| 2022-23 | 0 | 0 | 26,505 | 26,505 |
| 2023-24 | 0 | 0 | 29,725 | 29,725 |
| 2024-25 | 0 | 0 | 27,283 | 27,283 |
| 2025-26 | 29,747 | 0 | 29,747 | 29,747 |

`defensive_contribution` is therefore observed in **four seasons only**: 2016-17, 2017-18, 2018-19
and 2025-26.

### 0.3 BUG — `team_id` is null in six seasons and wrong in four

Measured against the invariant "a fixture's two teams are exactly the two distinct
`opponent_team_id` values, and a row's own team is whichever of those two is not its opponent":

| Season | Rows | Null `team_id` | Wrong `team_id` | % wrong |
|---|---:|---:|---:|---:|
| 2016-17 | 23,679 | 0 | 458 | 1.9% |
| 2017-18 | 22,467 | 0 | 406 | 1.8% |
| 2018-19 | 21,790 | 0 | 85 | 0.4% |
| 2019-20 | 22,501 | 0 | 131 | 0.6% |
| 2020-21 | 24,365 | **24,365** | 0 | — |
| 2021-22 | 25,447 | **25,447** | 0 | — |
| 2022-23 | 26,505 | **26,505** | 0 | — |
| 2023-24 | 29,725 | **29,725** | 0 | — |
| 2024-25 | 27,283 | **27,283** | 0 | — |
| 2025-26 | 29,747 | **29,747** | 0 | — |

**Two independent causes.**

*Nulls (2020-21 → 2025-26).* vaastav's `merged_gw.csv` changed schema: eras E1–E3 carry a numeric
`team_id`, eras E4+ carry the own-team as a *name* string (`team`) instead. `staging/vaastav.py:434`
documents the intent — "the name-based team-id join stays in facts assembly, deliberately". Facts
assembly does implement it (`facts/player_fixture.py:151-160`) via `_team_id_lookup`, which reads
`staged/teams`. But `staged/teams` was only ever built for 2026-27, so the lookup returns `None`
and line 160 silently fills the column with all-nulls.

*Wrong values (2016-17 → 2019-20).* E1–E3 derive `team_id` by joining `players_raw.csv`, which is an
**end-of-season snapshot**. Players who transferred mid-season are attributed to their final club for
every fixture of the season. Example: fixture 3 of 2016-17 has 7 distinct `team_id` values for a
match between two teams.

**The fix needs no external data.** The invariant holds universally — all **3,800 fixtures across all
ten seasons have exactly two distinct `opponent_team_id` values**, zero exceptions. Deriving
`team_id` from that invariant repairs both causes at once, for all ten seasons, with no dependency on
`staged/teams`.

### 0.4 `team_id` is not stable across seasons; `code` is

FPL reassigns `team_id` alphabetically every season:

| `team_id` | 2020-21 | 2022-23 | 2025-26 |
|---|---|---|---|
| 1 | Arsenal (code 3) | Arsenal (3) | Arsenal (3) |
| 2 | Aston Villa (7) | Aston Villa (7) | Aston Villa (7) |
| 3 | **Brighton (36)** | **Bournemouth (91)** | **Burnley (90)** |
| 4 | **Burnley (90)** | **Brentford (94)** | **Bournemouth (91)** |

`code` is stable. Any cross-season team feature must use `code`, never `team_id`.
**34 distinct clubs** appear across the ten seasons (20 per season plus promotion/relegation churn).

### 0.5 Raw data for the backfills is already on disk

| Raw table | Seasons captured | Missing spec columns |
|---|---|---|
| `vaastav/teams` | 2019-20 → 2025-26 | none |
| `vaastav/fixtures` | 2018-19 → 2025-26 | none |
| `vaastav/players_raw` | 2016-17 → 2025-26 | — |
| `vaastav/merged_gw` | 2016-17 → 2025-26 | — |

Both `teams.csv` and `fixtures.csv` carry every column that `TEAMS_SPEC` and `FIXTURES_SPEC`
(`staging/fpl_api.py`) require, for every season they cover. `stage_vaastav_source` currently stages
only `merged_gw`; it ignores the `teams.csv` and `fixtures.csv` it already captures.

2016-17 and 2017-18 have no vaastav `fixtures.csv`, but a fixtures table for those seasons is exactly
reconstructible from `facts/player_fixture` + `staged/player_fixture_stats`
(`fixture_id`, `event`, `kickoff_time`, home/away teams via the §0.3 invariant plus `was_home`, and
`team_h_score`/`team_a_score`).

### 0.6 Gameweek deadlines are derivable for all ten seasons

`deadline(G) = min(kickoff_time in G) − 1 hour`. Verified across all ten seasons:

| Season | GWs | Max span (h) | Overlapping GWs | Max fixtures in a GW |
|---|---:|---:|---:|---:|
| 2016-17 | 38 | 144 | 0 | 15 |
| 2017-18 | 38 | 143 | 0 | 16 |
| 2018-19 | 38 | 103 | 0 | 15 |
| 2019-20 | 38 | 271 | 0 | 12 |
| 2020-21 | 38 | 175 | 0 | 17 |
| 2021-22 | 38 | 358 | 0 | 16 |
| 2022-23 | **37** | 242 | 0 | 16 |
| 2023-24 | 38 | 240 | 0 | 13 |
| 2024-25 | 38 | 271 | 0 | 12 |
| 2025-26 | 38 | 192 | 0 | 13 |

**Zero gameweeks overlap** in any season, so the deadline is unambiguous. Note 2022-23 has 37
gameweeks (World Cup rescheduling) and gameweeks can span up to ~15 days, so deadline-frozen features
are legitimately stale for late fixtures in a congested gameweek — this matches reality and is
correct.

### 0.7 BUG — ClubElo ratings are stamped with the *fetch* date, not the *rating* date

Verified live on 2026-08-13. This blocks the elo backfill and has already corrupted one staged
season.

`ClubEloConnector.artifact_for_ratings` correctly records the requested rating date in
`meta.json` as `params.date`. But `write_raw` partitions the capture by `artifact.fetched_at`
(`storage/raw_io.py:156`), and `stage_clubelo_source` then recovers the rating date from the
*partition directory name*:

```python
as_of_date = partition_as_of(partition).date()   # staging/pipeline.py:322
staged = stage_ratings(body, as_of_date, season)
```

`partition_as_of` returns the wall-clock time of the fetch, not the date that was queried. The two
coincide for the daily cron (fetch today's ratings today), which is why this has gone unnoticed.

**Already-corrupted data.** The 2025-26 capture was fetched on 2026-08-03 with
`params.date = 2026-05-15`, and is staged as:

| club | elo | valid_from | valid_to | as_of_date (stamped) |
|---|---:|---|---|---|
| Arsenal | 2059.57 | 2026-05-11 | 2026-05-18 | **2026-08-03** |
| Aston Villa | 1888.91 | 2026-05-11 | 2026-05-15 | **2026-08-03** |

The validity windows prove these are mid-May ratings. The label is wrong by ~2.5 months.

**Why this blocks the backfill outright.** `facts/team_fixture._elo_for_team` implements the T-1
leakage guard by filtering and sorting on `as_of_date`:

```python
candidates = elo.filter(
    pl.col("club").is_in(club_names) & (pl.col("as_of_date") < kickoff_calendar_date)
)
```

It never reads `valid_from`/`valid_to`. If all 1,153 historical dates are fetched today, every row is
stamped `2026-08-13`, which is later than every historical kickoff — so the filter matches nothing
and **`elo_rating` would be 100% null across all ten seasons**. Confirmed empirically: for the
2025-26 staged ratings against the latest 2025-26 kickoff (2026-05-24), *no* rating passes the
`as_of_date < kickoff` test.

The backfill would write 1,153 files, stage without error, and silently produce nothing usable.

**Fix:** stage the rating date from `meta.json`'s `params.date` (already recorded, no re-fetch
needed) rather than from the partition name, and restage the existing 2025-26 capture.

Related observation, not yet diagnosed: `facts/team_fixture/season=2026-27` currently has
`elo_rating` null in 114/760 rows (15%) and `odds_implied_win_prob` null in 760/760 rows. The latter
is expected — there is no `footballdata` partition for 2026-27 yet — but it means the odds path is
unproven end to end and should be verified during Phase 0 on a completed season.

### 0.8 Target distribution (2024-25)

- 27,283 rows, 784 players, 380 fixtures
- **57.6%** of rows are 0-minute (player did not play)
- Mean minutes 27.4; 7,820 rows at 60+ minutes

| Bucket | All rows | % | Of those who played |
|---|---:|---:|---:|
| zeros (0 pts) | 16,405 | 60.1% | — |
| blanks (1-3) | 8,212 | 30.1% | 71.0% |
| tickers (4-8) | 1,891 | 6.9% | 16.3% |
| haulers (9+) | 660 | **2.4%** | **5.7%** |
| negative | 115 | 0.4% | — |

Haulers — the outcome we care most about — are 2.4% of rows. This severe imbalance is the direct
justification for bucketed evaluation rather than a single pooled RMSE.

---

## Section 1 — Q&A (verbatim)

**Q1.** Your list spans the whole ML lifecycle (EDA → baseline → candidate models → tuning → final
test). That's a lot for one task. What scope do you want this first task to cover?

**A1.** "EDA / data-evaluation and a simple baseline model for one phase, and then the basic sketch
for the next phase of model training"

---

**Q2.** The design spec (§4) is emphatic: "never model total points directly" — model each component
stat and sum through the scoring rules. For this first prototype, which single target should the EDA
and baseline focus on?

**A2.** "All five components at once (minutes, goals_assists, clean_sheet, bonus,
defensive_contribution)"

---

**Q3.** Given `features.build()` cannot produce a historical training set, how should we source the
training matrix for the prototype?

**A3.** *(User first asked for clarification: "wait, help me understand what you're saying.
facts/player_fixtures **does** have 10 seasons of data, or it's missing something which means we
can't build features for it?" — after the explanation in §0.1 above, the user selected:)*
"Also backfill facts/team_fixture for 10 seasons first (odds + congestion recoverable from data
already on disk; elo needs a re-pull)"

---

**Q4.** How do you want to handle this data-correctness fix relative to the model prototype task?

**A4.** "Fold it in as Phase 0 of this task — one plan, fix then EDA then baseline"

---

**Q5.** ClubElo is staged for only 2025-26 and 2026-27. Its API serves historical ratings by date, so
a 10-season backfill is possible but means a lot of network calls. Include it in Phase 0?

**A5.** "Include the elo backfill in Phase 0 — pull ClubElo history for all 10 seasons"

---

**Q6.** How should we split train/validation/test for the prototype? (Your list specifies 70/15/15,
but the spec mandates walk-forward backtesting, and a *random* 70/15/15 leaks badly on panel
time-series data.)

**A6.** "Chronological 70/15/15 by season: train early seasons → validate → test most recent, held
out once"

---

**Q7.** Which season boundaries do you want, and how should defensive_contribution be handled?

**A7.** "Split B: train 2016-17→2023-24, val 2024-25, test 2025-26 — closest to intent, most recent
season untouched"

---

**Q8.** You picked Split B but didn't pick a defensive_contribution variant — so under Split B, the
DC model trains only on 2016-19 and is tested on 2025-26. Is that what you want?

**A8.** "Same split as everything else — DC trains on 2016-19, tests on 2025-26; report it as the
era-continuity experiment"

---

**Q9.** At what level should the prototype be evaluated?

**A9.** "Both: per-component metrics for diagnosis, plus summed-points metrics bucketed by outcome
category as the headline"

---

**Q10.** What thresholds should define the outcome buckets for total points?

**A10.** "zeros = 0, blanks = 1-3, tickers = 4-8, haulers = 9+"

---

**Q11.** What should the Phase A baseline be? A naive "predict the player's recent rolling mean"
model is the floor any real model must beat; a regularised linear/logistic model is the interpretable
benchmark you described.

**A11.** "Both: a naive rolling-mean/persistence floor, plus ridge/logistic regression as the
interpretable model"

---

**Q12.** The repo currently has no ML or plotting dependencies at all (just polars, httpx, typer).
What should Phase A add to pyproject.toml?

**A12.** "numpy, scikit-learn, matplotlib, scipy — stay polars-native, no pandas"

---

**Q13.** The spec says notebooks/ is "exploration only, never imported, outputs stripped on commit" —
so notebook findings wouldn't persist. How should the EDA be delivered?

**A13.** "Reproducible module + CLI command (e.g. `fpl eda`), figures written to disk, findings
written up in a committed markdown report"

---

**Q14.** Mandatory check: is this production code or prototyping? Note Phase 0 touches core
staging/facts code and the committed data record, whereas Phase A is exploratory by nature.

**A14.** "Phase 0 full TDD (it touches facts/staging and the committed data record); Phase A lighter
— tests for the dataset builder and split logic, but not for plots"

---

**Q15.** The spec prescribes src/fpl/training/ for fit/evaluate code. Where should the new modules
and outputs live?

**A15.** "src/fpl/training/{dataset,splits,eda,baseline}.py, figures to data/eda/, report to docs/"

---

**Q16.** Understat is staged for all 10 seasons but never joined into facts. FPL's own
expected_goals only exists from 2022-23, so Understat would extend xG/xA back 6 more seasons — but
that join is its own sizeable task. In scope?

**A16.** "Defer Understat — scope is already large; note it as the top Phase B candidate"

---

**Q17.** Should goals and assists be modelled separately or as one combined target?

**A17.** "Separate models for goals and assists — different processes, different features matter"

---

**Q18.** FPL awards defensive-contribution points on a threshold (10 CBIT for DEF, 12 CBIRT for
MID/FWD). Should we model the raw DC count and threshold it, or model the binary threshold-met
outcome directly?

**A18.** "Model the raw DC count, then apply the positional threshold when scoring"

---

**Q19.** The scoring rules need `goals_conceded` as a count anyway (−1 per 2 conceded for GK/DEF),
and clean sheet is just `goals_conceded == 0 AND minutes >= 60`. Should the "clean_sheet" component
actually be a goals_conceded count model?

**A19.** "Model goals_conceded as a count, derive clean sheet from it + the minutes model"

---

**Q20.** How should the remaining scoring components (saves, cards, penalties, own goals) be handled
for the summed-points metric?

**A20.** "Use the naive rolling-mean predictor for the minor components (saves, cards, penalties, own
goals) so points still sum end-to-end"

---

**Q21.** Which scoring ruleset should the summed-points metric use? Actual total_points is under each
season's own rules, so comparability argues for own-era scoring — but deployment will be under
2026-27 rules.

**A21.** "Score each season under its own era's ruleset, so predicted and actual points are directly
comparable"

---

**Q22.** How should the baseline handle era-masked features, given ridge/logistic can't take NaN?

**A22.** "Baseline trains on era-complete features only; masked features carry median-impute +
explicit missingness indicator, reported separately"

---

**Q23.** `team_id` is not stable across seasons (ID 3 = Brighton in 2020-21, Bournemouth in 2022-23,
Burnley in 2025-26), so using it as a cross-season feature would be actively harmful. How should team
identity be handled?

**A23.** "Add stable team_code/opponent_team_code to facts in Phase 0, and use those for any team
feature"

---

**Q24.** OpenFPL's validated design uses position-specific models. Should the baseline fit one model
per component pooled across positions, or separate models per position?

**A24.** "Position-specific models per component, following OpenFPL"

---

**Q25.** What prediction horizon should the prototype target?

**A25.** "Models predicts at the fixture level, but always as if they were from the period before the
teams must be fixed, which I believe is one hour before the first match of the week in the premier
league. We then will have code to aggregate double/blank gameweeks after the models have made their
predictions, to advise on transfers or who to start"

---

**Q26.** 57.6% of rows are 0-minute and haulers are only 2.4% of rows. Should the component models
train on all rows, or conditionally on players who actually played?

**A26.** "Two-stage: minutes model on all rows; component models conditional on minutes>0, combined
as P(play) × E[stat | play]"

---

**Q27.** For the linear baseline, how should team identity be encoded? After Phase 0 we'll have elo,
odds and congestion, which arguably already capture team strength.

**A27.** *(User first asked: "Remind me, what's the features for team identity? You mention 80 sparse
columns?" — after the explanation that one-hot encoding 34 distinct clubs for own team + opponent
gives 68 sparse binary columns, with noisy coefficients for one-season clubs and an all-zero encoding
for unseen promoted sides, the user selected:)*
"Skip team one-hots — rely on elo + odds + congestion"

---

**Q28.** For the correlation / multicollinearity step you listed, should the EDA just report and flag
redundant features, or actually drop them?

**A28.** "Report Spearman + Pearson + VIF and mutual information, flag |r|>0.9 pairs, but only
auto-drop within the baseline pipeline — keep facts/features untouched"

---

**Q29.** What CLI commands should Phase A add?

**A29.** "`fpl dataset` (build training matrix), `fpl eda` (report + figures), `fpl baseline` (fit +
evaluate)"

---

**Q30.** `fpl dataset` will produce a ~253k-row feature matrix. data/ is normally the committed
record, but this is fully derived and would be a large binary in git. Where should it go?

**A30.** "Write to data/training/ but add it to .gitignore — it's fully reproducible from facts, and
would be a large binary in git"

---

**Q31.** Should the EDA figures in data/eda/ be committed?

**A31.** "Commit the figures as PNGs so the docs report renders on GitHub"

---

**Q32.** How detailed should the Phase B sketch be?

**A32.** "A section in the plan doc: candidate algorithms from the literature, tuning strategy,
walk-forward harness, model registry/artefact contract, and the single final test run"

---

**Q33.** The repo has a `fpl check` quality-gate system. Should Phase 0 add gates so this class of
bug can't silently recur?

**A33.** "Yes — add gates for 'exactly 2 teams per fixture', 'team_id non-null', and 'team_id !=
opponent_team_id'"

---

**Q34.** You said "ridge/logistic", but most of these targets are counts. sklearn has
PoissonRegressor, which is a better-matched GLM and still fully interpretable. Which do you want?

**A34.** "Match the link to the target: Ridge for minutes, PoissonRegressor for count targets (goals,
assists, goals_conceded, DC, bonus)"

---

**Q35.** Is there anything left to clarify before I write the plan document?

**A35.** "Nothing left — write the plan doc"

---

## Section 2 — Implementation plan

### Conventions binding every step

- Tests run with `$env:PYTHONPATH="src"; python -m pytest ...` (the package is not pip-installed
  here; `poetry` does not exist). Pytest treats warnings as errors.
- `ruff` is unavailable in this environment (install fails with a permission error on `ruff.exe`).
  Write to the configured style by hand: line length 100, rules `E,F,I,UP,B,SIM`.
- Polars parquet round-trips **drop the UTC timezone annotation**. Any module reading a `kickoff_time`
  parquet column and comparing it to a tz-aware value must first guard with:
  ```python
  if frame.schema["kickoff_time"].time_zone is None:
      frame = frame.with_columns(pl.col("kickoff_time").dt.replace_time_zone("UTC"))
  ```
- Phase 0 steps are **full TDD**: write the failing test first, then the implementation.
- Phase A steps are lighter: tests required for `dataset.py` and `splits.py`, not for plotting code.

---

### Phase 0 — data correctness

**Step 1.** Add a failing test `tests/facts/test_player_fixture_team_id.py::test_team_id_derived_from_fixture_invariant`.
Build a small synthetic `player_fixture_stats` frame for one fixture with two teams, where the
`team`/`team_id` source column is absent or wrong, and assert that
`build_player_fixture_facts` returns rows whose `team_id` is the opposite team to each row's
`opponent_team_id`. Expected outcome: test fails.

**Step 2.** In `src/fpl/facts/player_fixture.py`, add a private helper
`_derive_team_id_from_fixture(stats: pl.DataFrame) -> pl.DataFrame`. For each `fixture_id`, collect
the distinct non-null `opponent_team_id` values; require exactly two; set each row's `team_id` to
whichever of the two is not that row's `opponent_team_id`. Raise `ValueError` naming the season and
fixture if any fixture does not have exactly two distinct opponents. Expected outcome: Step 1's test
passes.

**Step 3.** In `build_player_fixture_facts`, replace the `_team_id_lookup` / name-join branch
(currently lines ~151-160) with an unconditional call to `_derive_team_id_from_fixture`, applied
after `opponent_team` has been renamed to `opponent_team_id`. Delete `_team_id_lookup` and its
import usage if nothing else references it. Expected outcome: `team_id` no longer depends on
`staged/teams` at all.

**Step 4.** Add a test asserting that an E1–E3-shaped input whose `team_id` column disagrees with the
fixture invariant is **corrected**, not preserved — this pins the 2016-17→2019-20 repair. Expected
outcome: test passes against the Step 2/3 implementation.

**Step 5.** Add `stage_vaastav_teams` to `src/fpl/staging/pipeline.py`, staging
`data/raw/vaastav/teams/season=*/` into `staged/teams` reusing the existing `TEAMS_SPEC` from
`staging/fpl_api.py`. Follow the shape of `stage_vaastav_source`: return `StageResult`, and return a
"no capture on disk" result rather than raising when the season is absent. Write a unit test with a
small in-memory CSV. Expected outcome: `staged/teams` buildable for 2019-20 → 2025-26.

**Step 6.** Add `stage_vaastav_fixtures` to `src/fpl/staging/pipeline.py`, staging
`data/raw/vaastav/fixtures/season=*/` into `staged/fixtures` reusing `FIXTURES_SPEC`. Same
conventions and a matching unit test. Expected outcome: `staged/fixtures` buildable for
2018-19 → 2025-26.

**Step 7.** Add a fixtures reconstruction path for 2016-17 and 2017-18 (no vaastav `fixtures.csv`).
New module `src/fpl/staging/fixtures_from_facts.py` deriving one row per `fixture_id` from
`staged/player_fixture_stats`: `event`, `kickoff_time`, home/away team ids (via the §0.3 invariant
combined with `was_home`), `team_h_score`, `team_a_score`, `finished=True`, `minutes`. `code` may be
null. Unit test on a synthetic two-fixture frame. Expected outcome: `staged/fixtures` buildable for
all ten seasons.

**Step 8.** Wire Steps 5–7 into the `fpl stage` CLI command and into `fpl backfill`, so a season's
`teams` and `fixtures` are staged alongside `player_fixture_stats`. Update `tests/test_cli.py`.
Expected outcome: `fpl stage --season 2021-22` produces all three tables.

**Step 9.** Add `team_code` and `opponent_team_code` to `facts/player_fixture`. Insert them into
`_COLUMN_ORDER` immediately after `team_id` / `opponent_team_id`. Populate by joining `staged/teams`
on `team_id` → `code` for the season; leave null (with a logged count) when `staged/teams` is absent.
Add a unit test. Expected outcome: stable cross-season team identity available in facts.

**Step 10.** Add three quality gates to `src/fpl/quality/checks.py` for the `player_fixture` facts
table: (a) every fixture has exactly two distinct `opponent_team_id` values; (b) `team_id` is
non-null for every row; (c) `team_id != opponent_team_id` for every row. Add unit tests for each gate
covering both the passing and failing case. Expected outcome: `fpl check` fails loudly if this class
of bug recurs.

**Step 11.** *(Prerequisite — see §0.7.)* Fix the ClubElo date-stamping bug. Add a failing test in
`tests/staging/test_clubelo_pipeline.py` asserting that a capture whose `meta.json` records
`params.date = "2018-03-10"` but which sits in an `as_of=2026-08-13T...` partition is staged with
`as_of_date = 2018-03-10`. Then change `stage_clubelo_source`
(`staging/pipeline.py:320-327`) to read the rating date from the capture's `meta.json`
`params.date`, falling back to `partition_as_of(partition).date()` only when `params.date` is absent
(so the two existing captures and any pre-existing cron captures still stage). Expected outcome:
rating dates are recovered from what was actually requested, not from when it was fetched.

**Step 12.** Restage the existing 2025-26 ClubElo capture and confirm `as_of_date` becomes
2026-05-15 (matching its `valid_from`/`valid_to` windows of 2026-05-10/11 → 2026-05-15/19) rather
than 2026-08-03. Expected outcome: the already-corrupted season is corrected.

**Step 13.** Add a quality gate asserting that every staged `clubelo_ratings` row has an
`as_of_date` falling within its own `valid_from`/`valid_to` window. This is a self-validating check
that would have caught §0.7 immediately, and will catch any recurrence. Add unit tests for the
passing and failing case.

**Step 14.** Add a multi-date ClubElo backfill command. The API serves historical dates correctly —
verified live on 2026-08-13 for 2016-08-12, 2018-03-10, 2020-12-26, 2023-04-15 and 2026-05-23, all
HTTP 200 with genuine point-in-time ratings whose `From`/`To` windows bracket the queried date.
Requirements:
- Derive the date list from `facts/player_fixture`: for each season, the set of
  `(kickoff_time − 1 day).date()` over distinct fixtures. **1,153 distinct dates** across ten
  seasons (2016-08-12 → 2026-05-23), 105–135 per season. The T-1 offset is the caller's
  responsibility, per the `sources/clubelo.py` module docstring.
- Skip dates already captured, by scanning existing partitions' `meta.json` `params.date`. Note
  `write_raw`'s existing skip is **content-hash based against the latest partition only**, which does
  not give date-level resumability — this must be an explicit pre-fetch check, or the run will
  re-fetch everything on resume.
- Budget ~2h10m: measured latency was 2.9–10.2s per request (mean ~6.8s), which dominates the
  configured `min_request_interval=2.0s` for the `clubelo` profile.
- Make it resumable and safe to re-run.

**Step 15.** Run the backfill for 2016-17 → 2024-25, then stage. Expected outcome: ten seasons of
`clubelo_ratings` staged, each row's `as_of_date` inside its validity window (Step 13's gate passes).

**Step 16.** Run the full rebuild over all ten seasons: `fpl stage` (teams, fixtures), `fpl facts`
(player_fixture rebuild), `fpl facts` (team_fixture build), `fpl check`. Verify explicitly that
`elo_rating` is **not** overwhelmingly null — per §0.7 that is the failure signature of the
date-stamping bug. Also verify the odds columns populate for completed seasons, which §0.7 notes is
currently unproven. Expected outcome: `facts/team_fixture` exists for all ten seasons;
`facts/player_fixture` has zero null `team_id`; all quality gates pass; points reconciliation still
passes (it does not depend on `team_id`).

**Step 17.** Commit Phase 0 as a single commit. The `data/` diff will be large (all ten seasons of
`player_fixture` rewritten, ten new `team_fixture` partitions, ~1,153 new ClubElo raw captures) —
that is expected and is the point of the change.

---

### Phase A — dataset, EDA, baseline

**Step 18.** Add `numpy`, `scikit-learn`, `matplotlib` and `scipy` to `[project.dependencies]` in
`pyproject.toml`. Do **not** add pandas. Expected outcome: `python -c "import sklearn, numpy, scipy,
matplotlib"` succeeds.

**Step 19.** Add `data/training/` to `.gitignore`. Leave `data/eda/` tracked. Expected outcome:
derived matrices stay out of git; figures are committed.

**Step 20.** Create `src/fpl/training/__init__.py` and
`src/fpl/training/deadlines.py` exposing `gameweek_deadlines(season, *, data_root=None) ->
dict[int, datetime]`, computing `min(kickoff_time) − 1 hour` per `event` from
`facts/player_fixture`. Raise if any two gameweeks' kickoff windows overlap. Unit test with a
synthetic two-gameweek frame plus an overlapping-gameweek failure case. Expected outcome: deadlines
available for every season.

**Step 21.** Create `src/fpl/training/dataset.py` with
`build_training_matrix(seasons, *, data_root=None) -> pl.DataFrame`. For each season and each
gameweek `G`, set `as_of = deadline(G)` and emit one row per player-fixture in `G`, whose features
are computed **only** from `facts/player_fixture` rows with `kickoff_time < as_of`. Reuse
`fpl.features.rolling.build_rolling_features` for the rolling block; join
`facts/team_fixture` for the team-context block. Include `season`, `event`, `fixture_id`,
`player_id`, `player_code`, `position`, `was_home`, `team_code`, `opponent_team_code`, the `obs_*`
masks, and label columns `label_minutes`, `label_goals_scored`, `label_assists`,
`label_goals_conceded`, `label_bonus`, `label_defensive_contribution`, `label_saves`,
`label_yellow_cards`, `label_red_cards`, `label_penalties_saved`, `label_penalties_missed`,
`label_own_goals`, `label_total_points_fpl`. Expected outcome: a ~253k-row matrix.

**Step 22.** Add `tests/training/test_dataset.py` covering: (a) row count matches the source facts
row count; (b) **no feature column is derived from any fixture at or after the gameweek deadline**
(the leakage test — construct a player whose only prior data is in gameweek `G` itself and assert
their rolling features are null); (c) labels are populated from the true outcome regardless of
`as_of`, matching the Phase 8 label contract. Expected outcome: tests pass.

**Step 23.** Add `fpl dataset` to `src/fpl/cli.py`, writing the matrix to
`data/training/matrix.parquet` via `paths`. Add a `paths.data_training_matrix()` helper mirroring
`paths.data_features_table()`. Update `tests/test_cli.py`. Expected outcome: `fpl dataset` produces
the matrix end to end.

**Step 24.** Create `src/fpl/training/splits.py` with `chronological_split(frame) ->
tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]` implementing Split B: train = 2016-17 → 2023-24,
validation = 2024-25, test = 2025-26. Expose the boundaries as module constants. Add
`tests/training/test_splits.py` asserting the partitions are disjoint, exhaustive, and strictly
ordered in time. Expected outcome: tests pass.

**Step 25.** Create `src/fpl/training/eda.py` implementing the data-evaluation sweep, computed
**on the training split only** so nothing leaks from validation or test:
missing-value counts per column (broken out by `obs_*` era); feature type classification
(numeric / categorical / ordinal / boolean); cardinality; per-feature variance and near-zero-variance
flags; skewness and kurtosis; outlier counts by IQR and by z-score; Pearson and Spearman correlation
matrices; VIF; mutual information against each target; and target-correlation rankings. Return
structured results as polars frames rather than printing.

**Step 26.** Add plotting to `src/fpl/training/eda.py` (or a sibling `eda_plots.py`): per-feature
histograms, target distributions with the zeros/blanks/tickers/haulers buckets marked, a correlation
heatmap, and missingness-by-season charts. Write PNGs to `data/eda/`. No tests required for plotting
code.

**Step 27.** Add `fpl eda` to `src/fpl/cli.py`, running Steps 25–26 and writing the figures plus a
generated markdown report to `docs/model-prototype-eda.md`. The report must state, in prose, the
findings for each item in Step 25, and flag every `|r| > 0.9` feature pair without dropping anything.
Expected outcome: a committed, readable EDA report with rendered figures.

**Step 28.** Create `src/fpl/training/baseline.py` with the naive floor: for each target, predict the
player's rolling mean over their most recent fixtures (reusing the existing rolling window
machinery). This is the benchmark every later model must beat. Add a unit test on a synthetic frame.

**Step 29.** Extend `src/fpl/training/baseline.py` with the GLM baseline. Build a scikit-learn
`Pipeline` per (component, position): `SimpleImputer(strategy="median", add_indicator=True)` →
`StandardScaler` → estimator, where the estimator is `Ridge` for `minutes` and `PoissonRegressor`
for `goals_scored`, `assists`, `goals_conceded`, `bonus` and `defensive_contribution`. Two-stage
structure: the minutes model trains on all rows; every component model trains only on rows with
`label_minutes > 0`, and predictions are combined as `P(play) × E[stat | play]`. Feature set excludes
team one-hots; era-masked features are excluded from the primary fit and reported separately.

**Step 30.** Create `src/fpl/training/evaluation.py`. Per-component metrics: MAE, RMSE, and Poisson
deviance for count targets. System metric: assemble predicted component values (using the naive
predictor for saves, cards, penalties and own goals), derive clean sheet via
`scoring.base.is_clean_sheet`, apply the positional DC threshold, and sum through the **season's own
ruleset** (`scoring/rules_legacy.py`, `rules_2025_26.py`, `rules_2026_27.py`) to get predicted total
points. Report RMSE and MAE overall and **within each outcome bucket** (zeros = 0, blanks = 1-3,
tickers = 4-8, haulers = 9+), plus Spearman rank correlation over each gameweek's player pool. Add a
unit test that a perfect component prediction reproduces the realised `total_points_fpl` exactly.

**Step 31.** Add `fpl baseline` to `src/fpl/cli.py`: build/load the matrix, split, fit both baselines
on train, evaluate on **validation only**, and write results to `docs/model-prototype-baseline.md`.
The command must **not** touch the test split. Update `tests/test_cli.py`.

**Step 32.** Run the full pipeline on real data: `fpl dataset`, `fpl eda`, `fpl baseline`. Record the
validation metrics per component, per position, and per outcome bucket in the report. Explicitly
report the defensive-contribution era-continuity result (trained 2016-19, evaluated on 2024-25) as
its own subsection.

**Step 33.** Add an "Implementation addendum" section to this document recording any deviations from
the plan, mirroring the convention established in
`.github/context/features-library-phase-8.md`.

**Step 34.** Run the full test suite (`$env:PYTHONPATH="src"; python -m pytest`) and confirm green.
Commit Phase A.

---

## Section 3 — Phase B sketch (not in scope for this task)

### 3.1 Prerequisite: the Understat join

The highest-value deferred item. `staged/understat_player_match` covers all ten seasons, but FPL's
own `expected_goals` only exists from 2022-23. Joining Understat into `facts/player_fixture` extends
xG/xA back six additional seasons — this is precisely the input that OpenFPL's validated results
depend on. 27 players remain unmatched in `identity/players_fpl_understat.csv`; they were
deliberately left out earlier and should be revisited here.

### 3.2 Candidate algorithms, from the repo's own prior-art review

| Source | Approach | Why it matters here |
|---|---|---|
| **OpenFPL** (arXiv:2508.09992, 2025) | Position-specific ensembles over FPL API + Understat only; "K-Best Search" selects ensemble members minimising validation RMSE; evaluated across zeros/blanks/tickers/haulers | Matched commercial FPL Review on **hauler RMSE** using exactly our source set. The benchmark that validates our source selection, and the direct model of our evaluation scheme. |
| **AIrsenal** (Alan Turing Institute) | Bayesian Poisson prediction feeding squad optimisation | Closest analogue to the whole system; a natural probabilistic upgrade path from the PoissonRegressor baseline. |
| **penaltyblog** | Dixon-Coles and bivariate Poisson goal models | Building block for team-level goal prediction, which feeds `goals_conceded` and clean sheets. |
| **FPL-Optimization-Tools** (sertalpbilal) | FPL as multi-period MILP, taking exogenous expected-points vectors | Composes directly with our predictions — the squad-selection layer. |
| **FPL Review Massive Data Model** | Commercial; uses betting odds and predicted lineups | The accuracy bar. Our odds features already exist after Phase 0; predicted lineups do not. |

Concrete candidates to train and compare: gradient-boosted trees (LightGBM / XGBoost — note these
handle NaN natively, so the era-masked features excluded from the Phase A baseline become usable),
random forests, and a hierarchical Bayesian Poisson model. Tree models also remove the need for
scaling and one-hot encoding, so the team-identity question reopens.

### 3.3 Tuning strategy

Bayesian optimisation (e.g. `optuna`) over the validation split, with the search space and the number
of trials both recorded in the artefact metadata. Grid search only for the small GLM
regularisation sweep. **Every tuning decision is made against validation, never test.**

### 3.4 Walk-forward harness

The spec (§9) mandates walk-forward backtesting as the production evaluation, using the identical
code path as the live run: step through historical deadlines, call the feature builder at each
`as_of`, predict, and score against reality. Phase A's chronological split is the prototype
approximation of this; Phase B builds the real thing. The `deadlines.py` module from Step 20 is
already the correct iteration primitive.

### 3.5 Registry and artefact contract

Per spec §9:
- `models/active.json` pointer file resolving the live artefact per component
- each artefact carries a `metadata.json` recording the exact ordered feature list, a hash of the
  feature registry, the scoring-rules version, the training `as_of` range, evaluation metrics, and
  the git SHA
- at inference, Actions rebuilds features and asserts schema match — **hard fail, not a warning**
- predictions archived append-only to `data/predictions/season=.../as_of=.../`, version-stamped
- monitoring appends per-model error, calibration, and **error by position and price bracket** to
  `data/monitoring/`
- one pinned environment (`uv` + `uv.lock`), because joblib artefacts are library-stack-coupled
- `src/fpl/training/` is imported by notebooks, never by Actions; `src/fpl/inference/` is imported by
  Actions and never fits

### 3.6 The single final test run

The 2025-26 test split is touched **exactly once**, after the winning model and its hyperparameters
have been selected on validation. The result is reported as-is, whatever it shows. Any subsequent
model change invalidates that number and requires a fresh held-out season.

### 3.7 Post-prediction aggregation

Per Q25/A25: models predict at fixture level as of the gameweek deadline. A separate layer aggregates
those fixture-level predictions across double and blank gameweeks to advise on transfers and
starting XI selection. This is a distinct component from the models themselves and is not part of the
modelling work.

### 3.8 Open risks carried forward

- **No season of overlap between 2018/19 and 2025/26** means nothing proves Opta's CBI/tackle/recovery
  definitions held constant across the seven-year gap. Phase A's DC era-continuity result is the
  first evidence either way.
- **Bonus is not derivable from rules in any season** (spec §17.4). A bonus model trained on the BPS
  input columns must be evaluated against FPL's actual `bps`/`bonus`, never against a hand-written
  formula.
- **Market data is only ever valid within its own season.** Price, ownership and transfer counts are
  collective responses to rules and must never be used as cross-season features.
