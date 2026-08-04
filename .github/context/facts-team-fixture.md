# Task: `facts/team_fixture` (Phase 7, tasks 12/13)

## Section 1 — Q&A (Verbatim)

**Q (assistant):** just did a pull request as we've got the github actions pulling the data at
regular intervals now. What do you think we should tackle next, understat->facts join or feature
library/gold layer?
**A (user):** [implicitly agreed with assistant's recommendation after it was given]

**Assistant recommendation:** tackle the Understat→facts join first (dependency order — Phase 8
reads from `facts/`), then Phase 8. Small, well-scoped, low risk vs. Phase 8's much bigger,
ambiguous scope.

**Q (assistant):** yes — invoked `start-new-task` skill to scope it properly.

During codebase exploration, the assistant discovered that `facts/team_fixture` (plan §7.13,
tasks 12/13 — Club Elo/odds/fixture-congestion, no Understat involved) **has not been built at
all yet** — it's a separate, already-fully-specified piece of Phase 7 that was never started, and
is distinct from any Understat player-xG join (which was never part of the original design's
column list for that table and has no existing fixture_id↔Understat match_id resolution).

**Q:** "I found something important: the plan's §7.13 `facts/team_fixture` table (using Club Elo,
fixture-congestion, and odds — tasks 12/13) hasn't actually been built yet at all. That's separate
from the Understat player-xG join, which was never in the original design's column list for that
table. Given this, which should we scope as the next task?"
Choices offered: (1) Build facts/team_fixture first (it's the actual remaining Phase 7 task per
the plan), (2) Scope the Understat player-xG facts join (new, unplanned work), (3) Do both in one
task.
**A:** "Build facts/team_fixture first (it's the actual remaining Phase 7 task per the plan, Club
Elo/odds/congestion)"

**Q:** "The plan's §7.13 already locks most decisions (grain, columns, silver-not-gold framing).
Should I follow that spec exactly as scoped, or are there any changes you want to make to it
before I turn it into an implementation plan?"
Choices: (1) Follow §7.13 exactly as written, no changes, (2) I want to change something first.
**A:** "Follow §7.13 exactly as written, no changes"

**Q:** "Is this for production code (full TDD, tests mirroring test_player_fixture.py, CLI wiring,
docs update) or a quicker prototype/ideation pass?"
Choices: (1) Production (full TDD, same rigor as tasks 10/11), (2) Prototype/ideation.
**A:** "Production (full TDD, same rigor as tasks 10/11)"

## Locked requirements (from plan §7.13, confirmed as-is by user)

- **New module:** `src/fpl/facts/team_fixture.py`.
- **Grain / primary key:** `(season, fixture_id, team_id)` — one row per team per fixture (two
  rows per fixture: home team's row and away team's row), mirroring `player_fixture`'s key
  discipline.
- **Dataclass/function shape** (from the plan, to be implemented as specified):
  ```python
  @dataclass(frozen=True)
  class TeamFixtureFactsResult:
      frame: pl.DataFrame
      rows: int
      unresolved_teams: tuple[str, ...]   # from any Tier 2 source, before crosswalk validation fails the build

  def build_team_fixture_facts(season: Season, *, data_root: Path | None = None) -> pl.DataFrame | None: ...
  def write_team_fixture_facts(season: Season, *, data_root: Path | None = None) -> TeamFixtureFactsResult: ...
  ```
  (Match `build_player_fixture_facts`/`write_player_fixture_facts`'s existing return-`None`-when-
  no-staged-data convention, and `FactsResult`'s `written`/`detail` pattern used elsewhere in the
  codebase — reconcile the plan's sketch with the actual established convention in
  `facts/player_fixture.py`.)
- **Columns:**
  - `elo_rating`, `opponent_elo_rating` — from Club Elo, using the already-implemented T-1 (day
    before kickoff) lookup rule (§7.2, `staging/clubelo.py`'s `clubelo_ratings` staged table,
    resolved via `team_external_ids.csv`'s `clubelo_name` column).
  - `fixture_count_prior_N_days` — a trailing-window fixture-congestion count combining FPL's own
    staged Premier League `fixtures` table with `openfootball`'s European schedules
    (`openfootball_fixtures` staged table), counting fixtures strictly before this fixture's
    kickoff. Domestic cups (FA Cup/EFL Cup) are **not** counted — no free source currently
    supplies them (tracked as `future-domestic-cup-source`, not this task's problem to solve).
    `N` is a **small fixed set: 7, 14, 28 days** — i.e. three columns
    (`fixture_count_prior_7_days`, `fixture_count_prior_14_days`, `fixture_count_prior_28_days`),
    not one column with a chosen window (Phase 8's lasso-style screening picks the useful window
    later).
  - `odds_implied_win_prob`, `odds_implied_draw_prob`, `odds_implied_loss_prob` — from
    `footballdata_matches_and_odds` staged table's Bet365 closing odds
    (`bet365_home_odds`/`bet365_draw_odds`/`bet365_away_odds`), converted to implied probabilities
    and **overround-normalised** (divide each raw implied probability, i.e. `1/odds`, by the sum of
    all three, per the plan's §7.6/§7.12 description). Resolved via `team_external_ids.csv`'s
    `footballdata_couk_name` column (short-form names, aliases separated by `"; "`).
- **Team-name resolution:** every Tier 2 source's own team-name string resolves to FPL's
  `team_code` (then to `team_id` via FPL's own staged `teams` table) through the already-built,
  hand-reviewed `crosswalk/team_external_ids.csv` (`identity/team_external_ids.py`,
  `load_team_external_ids`) — **no new crosswalk work needed**, this table already exists and is
  committed. A team-name string with no crosswalk row is collected into
  `TeamFixtureFactsResult.unresolved_teams` for the caller/CLI to report, mirroring the existing
  "unmapped-with-activity is a hard fail" discipline used for the player crosswalk (via
  `unmapped_source_names` in `identity/team_external_ids.py`) — surfaced, not silently dropped.
- **No-leakage discipline** (must hold, and be tested):
  - Elo lookup must be **T-1** (the rating as published on the day *before* the fixture's kickoff
    date), never same-day or later — mirrors `staging/clubelo.py`'s existing T-1 convention from
    §7.2 (already implemented there; this task's job is to *apply* it at the join, not re-derive
    it).
  - `fixture_count_prior_N_days` windows must count fixtures **strictly before** kickoff, never
    including the fixture itself or anything same-day-or-later.
- **This table is silver, not gold** — no rolling windows beyond the small fixed 7/14/28-day set
  above (which describe what happened, not an engineered as-of feature), no point-in-time
  construction beyond the T-1/strictly-before rules already listed, no modelling. Phase 8 will read
  it exactly as it reads `facts/player_fixture`.
- **Missing-source handling:** a fixture with no recorded Tier 2 data for one source (e.g. a club
  with no European involvement that season, so no `openfootball` congestion contribution beyond
  the FPL-only count; or a season/date Club Elo has no rating for) still produces a row — nulls in
  only that source's columns, **never a dropped row** (mirrors `player_fixture`'s "never a null
  row for an unrecorded fixture, but never drop a row for a partially-unrecorded source" rule).
- **Quality gate:** add a `FACTS_TABLE_GATES["team_fixture"]` entry in
  `src/fpl/quality/checks.py` with `unique_key(["season", "fixture_id", "team_id"])` at minimum
  (reusing `quality/gates.py` directly, as the plan specifies) — this hooks automatically into the
  existing `check_facts_tables`/`fpl check --layer facts` machinery, no new CLI plumbing needed
  there.
- **CLI wiring** (`src/fpl/cli.py`, §7.14): extend the existing `facts` command to also build/write
  `team_fixture` facts (mirroring how it currently calls `write_player_fixture_facts` then
  optionally `write_points`), and extend `backfill` similarly if in scope for parity with
  `player_fixture`'s existing backfill integration — confirm exact CLI surface during
  implementation by following the existing `player_fixture`/`points` pattern precisely, since the
  plan's own §7.14 snippet (`fpl facts team_fixture --season …`) already documents the target shape
  (second positional/optional table argument on `fpl facts`, backwards compatible with
  `player_fixture` staying the default).
- **Tests** (plan's own spec, mirror `tests/facts/test_player_fixture.py`'s structure and
  `tests/identity/test_team_external_ids.py`'s existing fixture-building helpers where useful):
  - A golden case: recorded/synthetic fixtures across all three Tier 2 staged tables
    (`clubelo_ratings`, `openfootball_fixtures`, `footballdata_matches_and_odds`) plus a real/
    synthetic FPL `fixtures` staged snapshot, asserting the assembled frame's values are correct
    (Elo T-1 pick, congestion counts, overround-normalised probabilities).
  - A key-uniqueness gate test: `unique_key(["season", "fixture_id", "team_id"])` passes on the
    golden case and fails when a duplicate is injected.
  - A "one Tier 2 source has no data for a given fixture" case (e.g. no European fixtures that
    season for a club) — row still exists, nulls only in that source's columns, never a dropped
    row.
  - A synthetic two-date Club Elo case proving the T-1 lookup picks the date *before* kickoff, not
    the fixture date itself (mirrors the existing `staging/clubelo.py` test already covering this
    at the staging layer — this test covers it again at the facts-assembly join, per the plan's own
    "phase 7 owns making sure `facts/team_fixture` itself cannot leak" framing in §7.15).
  - A congestion-window test proving `fixture_count_prior_N_days` never counts same-day-or-later
    fixtures.
  - `no_staged_data_returns_none` case, mirroring `test_player_fixture.py`'s existing test of that
    name.
- **Exit criteria** (plan §7.15, to re-verify at the end): `facts/team_fixture` written for at
  least the current season, key-unique, with `elo_rating`/`fixture_count_prior_N_days` never
  referencing same-day-or-future information — confirmed by the tests above, not just by manual
  inspection.
- **Documentation:** update `docs/superpowers/plans/2026-08-02-fpl-data-layer-phase-7-plan.md`'s
  §7.13/§7.14/§7.15 with a dated "implementation complete" note once done (matching the pattern
  used for every other completed §7.x section in that file), and update the task table (task
  12/13's row) if the doc has an open items/task-status table.

## Section 2 — Step-by-Step Implementation Plan

1. **Read and confirm existing building blocks before writing new code.** Re-read
   `src/fpl/facts/player_fixture.py` (the exact `FactsResult`/None-on-missing-data convention to
   mirror), `src/fpl/identity/team_external_ids.py` (`load_team_external_ids`,
   `unmapped_source_names`), `src/fpl/staging/clubelo.py`, `src/fpl/staging/footballdata.py`,
   `src/fpl/staging/openfootball.py` (their staged table schemas — column names/types exactly as
   currently staged), `src/fpl/staging/fpl_api.py`'s `fixtures` TableSpec (key `fixture_id`,
   columns `team_h`/`team_a`/`kickoff_time`/`event`), and `src/fpl/storage/paths.py`'s
   `staged_table`/`facts_table` helpers. No code changes in this step — just confirming exact
   column names/types to join against, to avoid a mismatch during implementation.

2. **Write failing tests first in `tests/facts/test_team_fixture.py`** covering every case listed
   above (golden case, key-uniqueness, missing-Tier-2-source-for-one-fixture, Elo T-1 pick,
   congestion-window strictly-before-kickoff, no-staged-data-returns-none, unresolved-team-name
   surfaced in `TeamFixtureFactsResult.unresolved_teams`). Use synthetic recorded-style fixtures
   (small, hand-built CSV/JSON bodies via `RawArtifact`/`write_raw`, mirroring
   `test_player_fixture.py`'s `_write_merged_gw` helper pattern) rather than live network calls.

3. **Implement `src/fpl/facts/team_fixture.py`**: `build_team_fixture_facts` (assembles the frame
   from staged tables + `team_external_ids.csv`, returns `None` if FPL's own `fixtures` table isn't
   staged for the season) and `write_team_fixture_facts` (writes
   `facts/team_fixture/season=.../part.parquet`, returns `TeamFixtureFactsResult`). Implement the
   Elo T-1 join, the three congestion-window counts, and the overround-normalised odds columns
   exactly per the locked requirements above. Run the test suite from step 2 until all pass.

4. **Add the quality gate**: extend `FACTS_TABLE_GATES` in `src/fpl/quality/checks.py` with a
   `"team_fixture"` entry (`unique_key(["season", "fixture_id", "team_id"])`, plus any additional
   leakage-relevant gate the tests from step 2 suggest is worth codifying as a reusable gate rather
   than only a one-off test assertion). Add/extend a test in `tests/quality/` if one exists for the
   equivalent `player_fixture` gate, mirroring its structure.

5. **Wire `fpl facts` and `fpl check` in `src/fpl/cli.py`**: extend the `facts` command to also
   call `write_team_fixture_facts` (following the plan's `fpl facts team_fixture --season …`
   shape, backwards-compatible with `player_fixture` remaining the default/other positional
   table), and confirm `fpl check --layer facts` already picks up the new gate automatically via
   `FACTS_TABLE_GATES` (no plumbing needed there beyond step 4). Add/extend
   `tests/test_cli.py` (or the project's existing CLI test module) coverage for the new `facts`
   invocation shape.

6. **Run the full test suite and `ruff format`/`ruff check`** across the whole repo, fix any
   failures, confirm the working tree is otherwise clean of unrelated changes.

7. **Run `fpl facts team_fixture --season <current>` (or the equivalent CLI invocation) against
   real staged data** for at least the current season to confirm it actually produces rows,
   inspect the output for sanity (row count, non-null coverage per column), and re-run
   `fpl check --layer facts` to confirm the new gate passes clean.

8. **Update `docs/superpowers/plans/2026-08-02-fpl-data-layer-phase-7-plan.md`** with a dated
   "implementation complete" note under §7.13 (mirroring the style of every other completed §7.x
   update note in that file), covering: what was built, the exact column list as implemented, any
   deviation discovered during implementation (if any), and the current row/coverage counts for
   the season(s) it was run against.

9. **Commit** the new module, tests, CLI wiring, quality gate, generated facts data (if the repo's
   convention is to commit generated facts parquet files — confirm by checking whether
   `facts/player_fixture` parquet files are currently committed before deciding), and the plan doc
   update, in one or more logically-scoped commits (mirroring the granularity used for tasks
   10/11: e.g. one commit for module+tests+gate+CLI, a separate commit if a real historical
   backfill run is done afterwards) — include the `Co-authored-by: Copilot` trailer per this
   repo's convention.
