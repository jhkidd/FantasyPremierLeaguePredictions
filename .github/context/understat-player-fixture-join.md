# Understat → `facts/player_fixture` join (Phase B, step 1)

## Section 1 — Q&A (verbatim)

**Context (from prior investigation, not asked as a question but load-bearing):**
- `src/fpl/staging/understat.py` stages `understat_player_match` (grain: one row per
  player per match), `understat_fixtures` (grain: one row per match), and
  `understat_players_season` (season aggregate, not used here).
- `facts/player_fixture.py`'s `build_player_fixture_facts` currently joins only staged
  `player_fixture_stats`; it has never referenced Understat.
- `data/crosswalk/players_fpl_understat.csv` (player_code ↔ understat_player_id) already
  exists: 2,643 rows, 1,845 matched, 798 unmatched — built/consumed via
  `src/fpl/identity/players_understat.py` and `fpl crosswalk refresh` /
  `crosswalk validate-understat`.
- `data/crosswalk/team_external_ids.csv` already has an `understat_name` column in its
  schema, but every row is currently empty — `src/fpl/identity/team_external_ids.py`'s
  `collect_source_names()` explicitly returns `[]` for Understat today with the comment
  "Understat (task 11) is not yet built, so its list is always empty for now."
- There is **no existing crosswalk between Understat's `match_id` and FPL's `fixture_id`**
  anywhere in the repo — confirmed by direct grep of `src/fpl/facts/*.py` (zero Understat
  references) and by reading `team_external_ids.csv` directly (understat_name column
  empty for every sampled row).
- Precedent for cross-source fixture matching without a shared id already exists in
  `src/fpl/facts/team_fixture.py`: football-data.co.uk and openfootball are both matched
  by team-name (via the reviewed crosswalk) + date. This task follows the same shape but
  drops the date requirement (see Q6).

**Q1. Existing FPL `expected_goals`/`expected_assists` columns already exist (populated
2022-23+, presumably Opta-sourced via FPL's own API). When Understat data is available for
those same seasons, should Understat values (a) fill only the pre-2022-23 gap, (b) always
overwrite FPL's own values, or (c) live in entirely separate `understat_*` columns so both
sources are visible for Phase B to choose between?**
A1. (c) Separate columns for both sources.

**Q2. For a `facts/player_fixture` row whose player has no entry in
`players_fpl_understat.csv` (798 currently unmatched), what happens to the new
`understat_*` columns for that row?**
A2. Null for that row (team-level join, if any, still happens — no dropped rows).

**Q3. Which Understat columns should be joined in — just the "expected" family
(`xg`/`xa`/`xg_chain`/`xg_buildup`) or the full per-match stat set (goals, shots,
key_passes, assists, cards, minutes, own_goals)?**
A3. Full set: `understat_goals, understat_own_goals, understat_shots, understat_xg,
understat_assists, understat_xa, understat_key_passes, understat_yellow_card,
understat_red_card, understat_xg_chain, understat_xg_buildup, understat_minutes`.
Dtypes mirror the staged `understat_player_match` dtypes (`pl.Int64` for counts/cards/
minutes, `pl.Float64` for `xg`/`xa`/`xg_chain`/`xg_buildup`).

**Q4. Should this task also populate the empty `understat_name` column in
`team_external_ids.csv` and wire Understat into the existing draft-then-review crosswalk
mechanism (`draft_team_external_ids` / `refresh_team_external_ids` /
`collect_source_names`), given that mechanism already accepts an `understat_names`
parameter but `collect_source_names` never supplies it?**
A4. Yes — proceed with wiring `collect_source_names` to also collect Understat's
distinct home/away team names (from staged/raw `understat_fixtures`), so
`fpl crosswalk refresh` drafts `understat_name` rows the same way it already does for
the other three sources.

**Q5. Should the hand-review of the drafted `understat_name` rows happen as part of this
task (agent runs `crosswalk refresh`, presents the draft for confirmation), or afterward
by the user independently?**
A5. As part of this task, but as a *separate* task/step after the code lands (see Q9) —
scoped to code+tests only for this immediate task; the user will run the real
`crosswalk refresh` + review + full facts rebuild themselves afterward as a follow-up.

**Q6. How should Understat fixtures be matched to FPL fixtures, given postponements can
shift Understat's date away from FPL's kickoff date by days?**
A6. Match by `(season, home_team_code, away_team_code)` alone — ignore date entirely.
Each home/away team pairing occurs at most once per season in the Premier League (no
replays), so this is unambiguous and immune to postponement-driven date drift.

**Q7. Should a matching `obs_understat` boolean be added alongside the new
`understat_*` columns, consistent with the existing `obs_defensive` /
`obs_bps_inputs` / `obs_expected` / `obs_starts` presence-mask pattern?**
A7. Yes.

**Q8. Should the facts build report Understat join coverage (e.g. "%
matched"), add a hard-fail quality gate, or neither?**
A8. Log a summary line only (e.g. "season 2024-25: N/M player-fixture rows matched to
Understat (X%)"), no hard-fail gate — `obs_understat` already gives per-row visibility.

**Q9. Production code or prototyping — and TDD?**
A9. Production code. Follow full TDD (failing test first, then implementation), same as
the rest of the facts layer.

**Q10. Scope boundary — does this task include running the real crosswalk refresh /
review / a full 10-season facts rebuild against real data?**
A10. No — this task is code + tests only. The user will run `crosswalk refresh`, review
the drafted `understat_name` rows, and rebuild `facts/player_fixture` for all ten seasons
as a separate follow-up step after this lands.

## Section 2 — Implementation plan

Team crosswalk side (wiring Understat into the existing draft-then-review mechanism):

1. In `src/fpl/identity/team_external_ids.py`'s `collect_source_names`, add logic to read
   staged/raw `understat_fixtures` for each season (mirroring the existing
   footballdata/openfootball blocks: use `paths.latest_partition("understat",
   "league_data", season, ...)`, `read_raw`, then `stage_understat_fixtures` — reuse
   `stage_fixtures` from `fpl.staging.understat`) and collect the distinct `home_team` /
   `away_team` strings into `understat_names`, replacing the current hardcoded `[]`.
   Update the module docstring's "Understat (task 11) is not yet built" note to reflect
   it now is.
2. Add/extend a test in `tests/identity/test_team_external_ids.py` (or create it if it
   doesn't exist) asserting `collect_source_names` returns non-empty `understat_name`
   entries when an `understat` `league_data` raw partition is present on disk, and `[]`
   when absent — write this test first (TDD), confirm it fails, then implement step 1
   to make it pass.

Understat match_id ↔ FPL fixture_id resolution + player_fixture join:

3. Write a failing test in `tests/facts/test_player_fixture.py` (or a new
   `tests/facts/test_player_fixture_understat.py`) asserting: given a small synthetic
   staged `player_fixture_stats` frame plus a synthetic staged `understat_player_match`
   / `understat_fixtures` pair plus a synthetic `players_fpl_understat` crosswalk and a
   synthetic `team_external_ids` crosswalk (with `understat_name` populated), the result
   of `build_player_fixture_facts` includes correctly populated
   `understat_goals/xg/xa/.../minutes` columns for matched rows, `obs_understat=True` for
   those rows, and null `understat_*` + `obs_understat=False` for (a) a player with no
   crosswalk row and (b) a fixture with no Understat match at all (e.g. season absent).
4. Add a second test case covering the team-pair matching itself: two Understat fixtures
   in the same season between the same two teams would be a data error — assert the
   join logic doesn't silently double-match (raises or logs, per existing convention of
   raising `ValueError` on corrupt-source invariants like `_derive_team_id_from_fixture`
   does) if `(season, home_team_code, away_team_code)` is not unique in a season's staged
   `understat_fixtures`.
5. Confirm both new tests fail (no implementation yet).
6. In `src/fpl/facts/player_fixture.py`, add a new private helper
   `_with_understat_columns(stats, season, *, data_root)` that:
   - Loads staged `understat_fixtures` and `understat_player_match` for the season (return
     early, leaving all `understat_*` columns null and `obs_understat=False`, if either
     partition is absent — mirroring `_with_team_codes`'s "absent input → null, log,
     don't fail" convention).
   - Loads `team_external_ids.csv` via `load_team_external_ids` and resolves each
     Understat fixture's `home_team`/`away_team` strings to `team_code` (splitting
     `ALIAS_SEPARATOR`-joined cells, same as `team_fixture.py`'s existing pattern).
   - Builds a `(season, home_team_code, away_team_code) -> match_id` lookup from
     `understat_fixtures`; raises `ValueError` if a team-pair is duplicated within a
     season (per step 4).
   - Loads `players_fpl_understat.csv` via `load_players_understat_crosswalk` to resolve
     each `player_code` to `understat_player_id`.
   - Joins `stats` (already has `team_code`/`opponent_team_id` by this point in the
     pipeline) to the fixture lookup via each row's own `team_code` +
     opponent's `team_code` (resolved the same way `was_home` already orders them) to get
     `match_id`, then joins `understat_player_match` on `(match_id,
     understat_player_id)` to pull in the stat columns, renaming Understat's own column
     names to the `understat_*`-prefixed names from Q3.
   - Sets `obs_understat` = row successfully matched (non-null `understat_xg` OR — more
     precisely — non-null `match_id` and non-null `understat_player_id`, since a real
     Understat row could theoretically have all-zero stats and still be a genuine match).
   - Logs one summary line per season: matched row count / total row count / percentage
     (Q8), at the same log level/style as the existing `_with_team_codes` warning.
7. Add `_EXPECTED_COLUMNS`-style tuple `_UNDERSTAT_COLUMNS` listing the twelve new
   column names in the order from Q3, and extend `_COLUMN_ORDER` to include them plus
   `obs_understat`, placed after the existing `_OBSERVED_FPL_COLUMNS` block (new data,
   not FPL's own observed output, so keep it clearly separated) and before the existing
   mask booleans, with `obs_understat` added alongside the other `obs_*` booleans at the
   end.
8. Wire `_with_understat_columns` into `build_player_fixture_facts`, called after
   `_with_team_codes` (needs `team_code` to already be resolved) and before the
   final `dupes` uniqueness check and `.select(list(_COLUMN_ORDER))`.
9. Run the new tests from steps 3-4 and confirm they now pass.
10. Run the full existing `tests/facts/` suite (`test_player_fixture.py`,
    `test_player_fixture_team_id.py`, `test_player_fixture_team_code.py`) to confirm no
    regression — the new columns/join must be purely additive.
11. Run `ruff check` and `ruff format --check` on the touched files; fix any drift.
12. Run the full test suite (`$env:PYTHONPATH="src"; python -m pytest`) and confirm green.
13. Update this doc's status / add a brief note to
    `.github/context/model-prototype-phase-9.md` §3.1 recording that the Understat join
    code has landed, and that the crosswalk refresh/review + full historical rebuild is
    still an outstanding follow-up (per A10) — do not mark §3.1 fully closed.
14. Report back to the user with: what was built, test results, and the exact follow-up
    steps they need to run themselves (`fpl crosswalk refresh`, review the drafted
    `understat_name` rows in `data/crosswalk/team_external_ids.csv`, then `fpl facts
    --season <each of 10 seasons>` to rebuild `player_fixture` with the new columns
    populated).
