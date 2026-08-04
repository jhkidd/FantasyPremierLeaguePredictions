# Phase 8 — Feature library (gold layer)

## Section 1 — Q&A (verbatim)

**Q: What's the actual end goal you want features FOR?**
A: Our ultimate goal is to have several models. We want to be able to have on our github pages app
a suggestion of which players to transfer/start, when to use specific chips, forecasted points for
the rest of the league, etc. The most fundamental model is the player point prediction per fixture.
We can sum the per feature scores to handle cases where there's several matches within a single
week, so no additional model needed there. Maybe consult some of the docs which I think contain
approaches from others which we can learn from for this.

*(Consulted `docs/superpowers/specs/2026-07-30-fpl-data-layer-design.md` §4, §9, Appendix A. Key
findings: features are a pure library — `build(as_of, horizon)`, never materialised, to make
train/serve skew structurally impossible; points must never be modelled directly — model each
component stat and sum through `scoring/rules_*.py`; component availability is tiered by era and
`facts/player_fixture` already carries a per-group availability mask that must not be silently read
as zero; OpenFPL — arXiv:2508.09992 — is the closest validated prior art: position-specific
ensembles on FPL API + Understat data, matched commercial FPL Review RMSE.)*

**Q: Feature library architecture — target OpenFPL's one-model-per-component from the start, or
stay model-agnostic?**
A: Feature library stays model-agnostic — just build the widest sensible feature set from facts/,
defer model architecture to a separate later task.

**Q: features.build(as_of, horizon) — single upcoming fixture per player, or a full gameweek
(0/1/2 fixtures per team)?**
A: Yes, we want the models to predict per-fixture. The rules for when you have to finalise your
team for a given gameweek is 90 minutes before the scheduled kick-off time of the first match in
that gameweek.

**Q: Rolling-window lengths — fixed set, season-to-date only, or many candidates for later lasso
pruning?**
A: Build several candidate windows (last 3/5/10 fixtures + season-to-date + last season) and let
later model-side feature selection prune them with something like lasso.

**Q: Team-level context features (Elo, congestion, odds) — simple per-fixture join, or something
more elaborate?**
A: team_fixture columns joined per-player via their team for that fixture (elo, opponent elo,
congestion, odds) — straightforward join, no aggregation needed.

**Q: Public entrypoint signature?**
A: `features.build(season, as_of, *, data_root=None) -> pl.DataFrame`, one row per
(player_id, fixture_id) for every fixture in the given season kicking off on/after as_of. Confirmed
this must work even when the target fixture hasn't been played yet — we need to be able to
construct features for the upcoming 2026/27 season specifically.

**Q: Confirm mechanics — features.build only needs the FPL fixtures schedule (known ahead of time)
plus history strictly before as_of; the target fixture itself never needs to have been played?**
A: Yes — confirmed.

**Q: Player with zero fixture history before as_of (brand new to the league) — include row with
null history features, or exclude entirely?**
A: Include the row with nulls for that player's history features, but still include team_fixture
context (transfer/new-signing case is common and shouldn't be dropped).

**Q: Player population for a given build call — every player in FPL's staged `players` table, or
only players whose team has a fixture in the window?**
A: Every player who appears in FPL's staged players table for that season (i.e. every player in the
game, regardless of team).

**Q: Mid-season transfers — use team at request time (current team), or team as of each historical
fixture's date?**
A: It should be the team at request time. We don't want to advantage the training data with
foreknowledge we won't have at execution.

**Q: Precisely: for backtesting a past as_of, use the team from the snapshot closest to (but not
after) as_of — true point-in-time — or today's players table for all historical as_of dates?**
A: Team as recorded in the players snapshot closest to (but not after) as_of — true point-in-time.

**Q: For future/target fixtures specifically (no snapshot history yet) — use FPL's current
`players` table team for ALL of that player's future-fixture rows in this call?**
A: Yes, use the players table's current team as the team for ALL of that player's future-fixture
rows in this build call (single team assumption per call) — confirmed no attempt to predict future
transfers.

**Q: If no snapshot exists at/before a past as_of (e.g. very early season) — how to resolve team?**
A: Fall back to facts/player_fixture's own recorded team_id for their most recent past fixture
before as_of, if any (more robust, reuses data already point-in-time-correct).

**Q: If NEITHER a snapshot at/before as_of NOR any facts/player_fixture history before as_of
exists (genuinely brand-new player) — fall back to live current team, or exclude entirely?**
A: Fall back to the snapshot nearest in the future, but we should have some logging function which
captures how frequently this happens and makes sure it's an edge case and not a systematic issue.

**Q: Should that logging be a simple log line, or aggregated into a returned diagnostics object?**
A: Aggregated into a returned diagnostics object.

**Q: Where should the feature library code live?**
A: `src/fpl/features/library.py` with `build()` as the main entrypoint, plus feature-group
submodules (rolling.py, team_context.py, availability.py etc.).

**Q: Which stats get rolling-window features built — every raw component column, or a curated
subset?**
A: Build rolling windows over every raw component stat already in facts/player_fixture (minutes,
goals, assists, clean_sheets, goals_conceded, saves, cards, defensive contribution inputs,
bonus/bps, etc.) plus derived points under the current ruleset.

**Q: Rolling-window aggregation of count stats — sum only, mean/per-90 rate only, or both?**
A: Both sum and mean/per-90 rate for every window (widest net, let lasso prune).

**Q: Do "last 3/5/10 fixtures" windows span season boundaries (pull from prior season to fill
out early-season windows), or reset at season boundary?**
A: Yes — windows like "last 3 fixtures" span season boundaries, counting a player's most recent N
fixtures regardless of season, using the cross-referenced facts/player_fixture history.

**Q: "Last season" window — full aggregate stats from the single most recent complete prior
season, or something else (e.g. per-90 rates)?**
A: Yes, single most recent complete prior season. If they only played half of the prior season,
aggregate what we do have.

**Q: How should a rolling window handle a mix of available/unavailable-per-era stats within it
(e.g. "tackles in last 10" spanning an era boundary)?**
A: Compute the rolling window only over fixtures where that stat group's availability mask is
true, and separately surface what fraction/count of the window was masked-out.

**Q: Should this task include writing the leakage test now, or defer to a follow-up task?**
A: Yes, write an automated leakage test as part of this phase 8 task.

**Q: Production code (full TDD) or prototype first?**
A: Yes, full production TDD (same rigor as facts/team_fixture).

**Q: Should this task add a CLI command, or stay library-only?**
A: `fpl features build --season ... --as-of ... [--horizon-gameweeks N]` writing a parquet snapshot
to data/features/ for inspection/debugging only (not authoritative — library stays the source of
truth).

**Q: Default value of --horizon-gameweeks?**
A: Default horizon = 1 gameweek (all fixtures for the single next unplayed gameweek, including
doubles/blanks naturally), overridable via --horizon-gameweeks.

**Q: Should features.build's output include realised target/label columns when available (past
as_of), or should labels always be joined separately by training code?**
A: Yes, include the realised label columns when available (features.build works for both
training-set construction and live inference this way).

**Q: Should features.build's output include player position and price-at-as_of as feature
columns?**
A: Yes, include player position and price-at-as_of as features too (both are known inputs at
prediction time, not leaky).

**Q: Anything else to clarify before writing the plan?**
A: No.

---

## Section 2 — Step-by-step implementation plan

### Building blocks to read first (no code changes)
1. Re-read `src/fpl/facts/player_fixture.py` in full (column groups, availability mask columns
   `obs_defensive`/`obs_bps_inputs`/`obs_expected`/`obs_starts`, `KEY` tuple) — this is the primary
   input table.
2. Re-read `src/fpl/facts/team_fixture.py` in full (just built) — this is the team-context input
   table, keyed `(season, fixture_id, team_id)`.
3. Read `src/fpl/facts/points.py` and `src/fpl/scoring/{base.py,rules_2026_27.py}` to understand
   how per-fixture points are derived from component stats — features must reuse this, not
   duplicate scoring logic.
4. Read `src/fpl/staging/fpl_api.py`'s `PRICE_SNAPSHOTS_SPEC`/`AVAILABILITY_SNAPSHOTS_SPEC` and
   `_stage_bootstrap_snapshot` to understand the `as_of_ts`-stamped snapshot tables used for
   point-in-time team/price/position resolution. Note neither snapshot table currently carries a
   `team` column — confirm this via `view` before assuming; if true, note that team resolution for
   snapshot-based lookups needs the corresponding `players` table snapshot instead (or in addition)
   — clarify in implementation which staged table actually carries team_id historically.
5. Read `src/fpl/storage/paths.py` and `src/fpl/storage/parquet_io.py` for the `data_root`-relative
   path conventions and parquet read/write helpers to reuse.
6. Read `src/fpl/quality/checks.py` for the `Gate`/`unique_key`/`run_gates` pattern (features table
   is not stored, so no new facts-table gate — this is read-only reconnaissance to confirm nothing
   needs registering there).

### Step 1 — Write failing tests for team resolution (point-in-time)
7. Create `tests/features/test_team_resolution.py`. Cover: (a) future target fixture uses current
   `players` table team for every row; (b) past as_of with a snapshot at/before it resolves that
   snapshot's team; (c) past as_of with no snapshot but facts/player_fixture history before it
   falls back to the most recent prior fixture's team_id; (d) a player with neither falls back to
   the nearest-future snapshot AND increments a diagnostics counter; (e) diagnostics object exposes
   a count/list of players who hit case (d).

### Step 2 — Write failing tests for rolling-window features
8. Create `tests/features/test_rolling.py`. Cover: last-3/5/10-fixture sum and per-90 rate for a
   count stat (e.g. goals_scored), spanning a season boundary (fixtures from two seasons
   contributing to one window); season-to-date aggregate; last-complete-season aggregate
   (including a partial-season case); a defensive-contribution-tier stat window that straddles an
   era boundary — assert the window aggregates only over mask-true fixtures and a
   masked-fraction/count column is present and correct; a brand-new player with zero prior history
   returns nulls for every rolling feature but the row is still present.

### Step 3 — Write failing tests for team-context join
9. Create `tests/features/test_team_context.py`. Cover: a player's row picks up their team's
   `elo_rating`/`opponent_elo_rating`/`fixture_count_prior_N_days`/`odds_implied_*_prob` for the
   target fixture from `facts/team_fixture`; missing `facts/team_fixture` data for a season yields
   nulls for these columns only, row still present.

### Step 4 — Write failing tests for features.build's overall contract
10. Create `tests/features/test_library.py`. Cover: one row per (player_id, fixture_id) for every
    fixture on/after as_of within the requested horizon (default 1 gameweek, including a
    double-gameweek producing 2 rows for one player and a blank-gameweek team producing 0); every
    player from the staged `players` table appears at least once if their team has any fixture in
    horizon; position and price-at-as_of columns present and correctly point-in-time resolved;
    realised label columns (e.g. actual minutes/points) populated when the target fixture has
    already been played and null when it hasn't; `data_root` plumbed through correctly; missing
    `facts/player_fixture` or missing staged `fixtures`/`teams` returns `None` (mirroring
    `build_team_fixture_facts`'s contract) with a detail message.

### Step 5 — Write the leakage test
11. Create `tests/features/test_no_leakage.py`. For a handful of hand-built fixture histories,
    assert that changing data strictly at-or-after `as_of` (a same-day result, a same-day Elo
    update, a same-day odds row) never changes `features.build`'s output for that `as_of` — i.e.
    perturb only rows with `kickoff_time >= as_of` or `as_of_date/match_date >= as_of` and re-run,
    diff must be empty.

### Step 6 — Implement the modules to make tests pass
12. Create `src/fpl/features/__init__.py`.
13. Create `src/fpl/features/team_resolution.py`: point-in-time team lookup per the agreed
    precedence (current `players` table for future fixtures → nearest-at-or-before snapshot for
    past as_of → most recent `facts/player_fixture` team_id before as_of → nearest-future snapshot
    as last resort) plus a `TeamResolutionDiagnostics` dataclass (or similar) tracking how many/
    which players hit the last-resort fallback.
14. Create `src/fpl/features/rolling.py`: rolling-window aggregation (sum + per-90 rate) over every
    `facts/player_fixture` component column for windows {3, 5, 10 fixtures, season-to-date, last
    complete season}, respecting each column's availability-mask group and emitting a masked-count/
    fraction companion column per masked window feature.
15. Create `src/fpl/features/team_context.py`: join `facts/team_fixture` columns onto each player
    row via their resolved team_id for the target fixture (and opponent team_id for
    opponent-side columns already present in team_fixture).
16. Create `src/fpl/features/availability.py` (or fold into rolling.py if simpler once written):
    house the availability-mask-aware windowing helper shared by rolling.py, since defensive/BPS/
    expected/starts masks each need the same masked-window logic.
17. Create `src/fpl/features/library.py`: the public `build(season, as_of, *, horizon_gameweeks=1,
    data_root=None) -> FeaturesResult` (or returns `None`, mirroring the facts pattern — confirm
    naming/shape while implementing, keeping consistent with `TeamFixtureFactsResult`/`FactsResult`
    naming conventions already in the codebase) that assembles fixtures in horizon, resolves teams,
    computes rolling features, joins team context, joins position/price, joins realised labels
    where available, and returns the diagnostics object from step 13 alongside the frame.
18. Run `tests/features/` repeatedly, fixing implementation until all pass (expect several rounds,
    consistent with the team_fixture task's experience).

### Step 7 — CLI wiring
19. Add a `features` Typer sub-app or command group to `src/fpl/cli.py`:
    `fpl features build --season ... --as-of ... [--horizon-gameweeks N]` that calls
    `features.library.build(...)`, writes the resulting frame to
    `data/features/season=.../as_of=.../part.parquet` (debugging snapshot only, not authoritative),
    and prints row count plus the team-resolution diagnostics summary.

### Step 8 — Full validation
20. Run the full test suite (`pytest -q`) and confirm no regressions.
21. Run `ruff check` and `ruff format --check` (then `ruff format`) on every new/changed file only.
22. Run `fpl features build` against real staged 2026-27 data for a real `as_of` (e.g. now) and
    inspect the output for sanity (row counts, non-null team-context columns, reasonable
    diagnostics counts).

### Step 9 — Documentation and commit
23. Add a new plan doc `docs/superpowers/plans/2026-08-04-fpl-data-layer-phase-8-plan.md` (or
    append a phase 8 section to the existing design spec, matching how phase 7 got its own plan
    doc) recording the design decisions made in this Q&A, the module layout, and the exit criteria.
24. Commit all new/changed files with the `Co-authored-by: Copilot` trailer.

## Production vs. prototype
Confirmed: production code, full TDD, same rigor as `facts/team_fixture` (tasks 12/13).

## Implementation addendum (post-hoc, recorded after real implementation)

Two design points changed from what the Q&A above originally agreed, discovered only once
implementation was underway — recorded here rather than editing the Q&A transcript itself, mirroring
how phase 7's plan doc got an "Update" note after its own real-data-run discoveries.

**Team resolution — snapshot tables don't carry a team column.** The Q&A above assumed
`price_snapshots`/`availability_snapshots` could resolve a point-in-time team. Neither table carries a
`team` column (confirmed against `PRICE_SNAPSHOTS_SPEC`/`AVAILABILITY_SNAPSHOTS_SPEC` in
`fpl/staging/fpl_api.py`). Revised precedence, approved mid-implementation: (1) if the target fixture
has already been played and a `facts/player_fixture` row exists for this player at this exact fixture,
use its own recorded `team_id`; (2) else use this player's most recent `facts/player_fixture` row
strictly before `as_of`; (3) else fall back to FPL's current `players`-table team-of-record, flagged via
`TeamResolutionDiagnostics.fallback_to_current_team`. Implemented in `features/team_resolution.py`.

**Season-to-date is a distinct frame, never "all of history".** An early draft of
`features/rolling.py` computed "season-to-date" as `window == len(history)`, which silently collapsed
into "all of history" (spanning season boundaries) — wrong, since the fixed 3/5/10-fixture windows are
season-spanning by design but season-to-date is explicitly season-scoped. Fixed by giving
`build_rolling_features` two extra, independent frame parameters — `season_to_date_history` and
`last_season_history` — alongside the season-spanning `history` used only for the fixed windows.
`features/library.py` passes the caller's own facts-filtered `history` as both `history` and
`season_to_date_history` for now (since the reference implementation only tracks one season of
"current" history per player); a future multi-season caller should filter `season_to_date_history` to
`season == target_season` explicitly.

**CLI command is `fpl features`, not `fpl features build`** — a single top-level command (the stub
already existed as `fpl features --as-of ...`), not a Typer sub-app, to match the existing surface named
in `test_help_lists_the_intended_surface`. Flags: `--season`, `--as-of` (defaults to now if omitted),
`--horizon-gameweeks` (default 1). Writes its debug snapshot to
`data/features/season=.../as_of=.../part.parquet` via the new `paths.data_features_table` helper.

### Final module layout
- `src/fpl/features/team_resolution.py` — point-in-time team lookup + `TeamResolutionDiagnostics`.
- `src/fpl/features/rolling.py` — rolling-window (3/5/10/season-to-date/last-season) sum + per-90 rate
  features, mask-aware for defensive/BPS-input/expected-stats column groups.
- `src/fpl/features/team_context.py` — simple per-fixture join of `facts/team_fixture` columns.
- `src/fpl/features/library.py` — the public `build(season, as_of, *, horizon_gameweeks=1,
  data_root=None) -> FeaturesResult` entrypoint, tying the above together plus position/price-at-as_of
  and realised label columns (`label_minutes`, `label_total_points_fpl` — always the true known outcome
  when it exists, regardless of `as_of`; only the feature columns are leakage-gated).
- Tests: `tests/features/test_team_resolution.py`, `test_rolling.py`, `test_team_context.py`,
  `test_library.py`, `test_no_leakage.py` — 35 tests total, all passing.

### Exit criteria met
- Full `pytest -q` suite green (no regressions).
- Leakage test suite (`test_no_leakage.py`) passes: perturbing any same-day-or-later fact never changes
  a feature column for a fixed `as_of`; only label columns (which intentionally reflect the true known
  outcome for training-set construction) are allowed to change.
- `fpl features` CLI command wired and covered by `tests/test_cli.py`.

