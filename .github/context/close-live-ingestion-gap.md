# Close the live-ingestion gap (subsystem 2)

Follow-up to `.github/context/model-prototype-phase-9.md` §3.5 and
`.github/context/subsystem3-close-and-mvp-site.md` steps 8/13: `facts/player_fixture`
has never carried rows for the current, in-progress season, so every rolling
"last 3/5/10 games" feature — and so every predicted component — comes back
null for it. `fpl predict` now skips archiving that case cleanly (this
session, commit `ee795b1`) rather than writing junk, but a real recommendation
stays blocked until this is closed.

## Diagnostic (2026-09-23)

- `FplApiConnector.event_live()` / `.element_summary()` already exist
  (`src/fpl/sources/fpl_api.py`) and are already reachable via
  `fpl ingest fpl --endpoint event-live --event N` / `--endpoint element-summary
  --player N`. **The gap is staging and automation, not fetching.**
- Checked the real, live FPL API (`/api/event/5/live/`) directly: it returns
  everything the trained model's *predicted components* need — `minutes,
  goals_scored, assists, goals_conceded, own_goals, penalties_saved,
  penalties_missed, yellow_cards, red_cards, saves, bonus, bps,
  defensive_contribution, clearances_blocks_interceptions, tackles,
  recoveries, starts, expected_goals, expected_assists,
  expected_goal_involvements, expected_goals_conceded, total_points`.
- It does **not** return ~15 detailed "BPS-input" columns (`attempted_passes,
  completed_passes, key_passes, big_chances_created, big_chances_missed,
  open_play_crosses, dribbles, tackled, fouls, offside, target_missed,
  errors_leading_to_goal(+attempt), penalties_conceded, winning_goals`).
  Checked our own archive: these are **already null for every season from
  2020-21 onward** (`2016-17` is the only season with real values), including
  the two most recently completed seasons (`2024-25`, `2025-26`). FPL retired
  this Opta-derived breakdown tier years ago; this is a pre-existing,
  permanent gap, not something this fix introduces.
- The codebase already has a purpose-built flag for exactly this:
  `obs_bps_inputs` (`src/fpl/facts/player_fixture.py:47-63,469-471`), already
  `False` for every row since 2020-21. Leaving these columns null for the
  newly-staged current season requires **zero new design** — the existing
  `obs_bps_inputs = any_horizontal(is_not_null(...))` computation already
  produces the correct, consistent result.
- `defensive_contribution`/`cbi`/`tackles`/`recoveries` (`obs_defensive`) and
  `expected_*` (`obs_expected`) **are** both live-API-native and populated in
  the most recent completed season (`2025-26`) — these must be correctly
  mapped from `event_live` so the current season matches that precedent
  (non-null).

**Decision:** no zero-fill, no new "unavailable" flag, no retrain. Stage only
what `event_live` actually provides; leave the rest null exactly as the
existing pipeline already does for 5+ seasons of history.

## Scope

1. ~~`event_status()` connector method~~ **not needed**: `bootstrap-static`'s
   `events` list already carries `finished` and `data_checked` per gameweek
   (confirmed against the real API and `tests/fixtures/fpl/bootstrap_static.json`),
   which is already ingested/staged every run. Only a `data_checked`
   `ColumnSpec` was added to `EVENTS_SPEC` (`src/fpl/staging/fpl_api.py`) —
   no new endpoint, connector method, or workflow step required.
2. A staging function, `stage_event_live()` (`src/fpl/staging/fpl_api.py`),
   turns one `event_live` raw capture into `facts/player_fixture`-shaped
   `player_fixture_stats` rows for the current season — mapping
   `stats.clearances_blocks_interceptions` → `cbi`, etc., deriving
   `was_home`/`opponent_team`/`position` via joins against the already-staged
   `players`/`fixtures` tables — leaving the ~15 BPS-input columns
   absent/null, matching every other season already on disk. Players with a
   double gameweek (>1 fixture in `explain`) are skipped and counted, since
   `event_live`'s `stats` block is a gameweek total, not per-fixture, and
   there's no way to split it without guessing. `facts/player_fixture.py`
   needed **zero changes** — it already reads this table generically and
   derives `team_id` purely from `opponent_team` via fixture pairing.
3. **Backfill design change from your original suggestion:** rather than
   `element-summary` per player (768+ calls for a full squad), reuse
   `event-live` per already-finished-but-uncaptured gameweek this season —
   the endpoint isn't "live-only", it serves any past gameweek's finalised
   stats too (this is literally how vaastav's own historical scrape works).
   That's 1 call per missing gameweek instead of 1 call per player.
   Implemented as `missing_event_live_captures()` (`src/fpl/ingest.py`):
   reads the latest on-disk `bootstrap_static` capture, filters to
   `finished and data_checked`, returns whichever of those gameweeks lack an
   `event_live` capture on disk. `ingest_fpl(..., endpoint="event-live")`
   with no `--event` now calls this and fetches every missing one — the same
   mechanism serves steady-state (1 new gameweek/week) and full-season
   backfill (many at once) with no code branch between them.
4. Wire into `daily-snapshot.yml`: add an "Ingest event-live" step after the
   existing FPL ingest step, calling
   `uv run fpl --verbose ingest fpl --endpoint event-live` (no `--event`).
   No further workflow changes needed — `stage`/`facts`/`check`/commit steps
   already run unconditionally and already read from the right places.
5. Tests (TDD): staging function tests (field mapping, `was_home`/position
   derivation, `obs_*` flags, double-gameweek skip, key uniqueness),
   pipeline end-to-end tests (raw capture → staged rows → facts build),
   ingest auto-discovery tests (fetch-missing, skip-already-captured, direct
   `missing_event_live_captures()` unit test).
6. Verify end-to-end for real: run the workflow, confirm
   `facts/player_fixture` gains current-season rows, then confirm a real
   `fpl predict && fpl optimise` run produces a genuinely non-null
   recommendation, and the deployed site shows it.

## Team-id resolution bug (found during real-data verification)

Step 6's real workflow run (`gh workflow run daily-snapshot.yml`) surfaced a
genuine correctness bug that no unit test with synthetic fixtures had caught:
`Check facts` failed with `not_null(team_id): 25 null value(s)`.

**Root cause:** `stage_event_live()`'s original design joined every stats row
to the *current* `players.team_id` to derive `was_home`/`opponent_team`.
Bootstrap-static's `elements[].team` field only ever reflects a player's
*current* club — for anyone who has since transferred, that misattributes a
past gameweek's fixture to their new club (or to neither side, if the new
club wasn't even playing that fixture). Confirmed concretely against real
2026-27 data: several players' current team didn't match either side of
fixtures they had genuinely played in under their old club. A "pick the
nearest-preceding daily `bootstrap_static` snapshot" fix alone wasn't fully
reliable either — bootstrap-static itself can lag a few days behind a
real-world transfer at any given capture instant.

**Fix — a validated 3-source waterfall**, most authoritative first, each
candidate accepted only if it actually equals the fixture's `team_h` or
`team_a` (a non-null-but-stale value must not shadow a better source further
down the list):

1. `match_side_team` — `{(fixture_id, player_id): team_id}` built directly
   from the fixture's own per-match `stats` breakdown (h/a element lists).
   Ground truth for anyone with recorded involvement; immune to
   bootstrap-static lag since it's real match-event data, not a roster
   snapshot.
2. `team_by_player` — `{player_id: team_id}` reconstructed from the nearest
   `bootstrap_static` daily capture at-or-before the gameweek's deadline
   (falling back to the earliest capture on disk for gameweek 1). Covers
   zero-involvement players (unused substitutes) absent from source 1.
3. `players.team_id` (current club) — last-resort fallback, mainly exercised
   by a mid-season signing's zero-stat placeholder rows for gameweeks before
   they'd even joined the league (no historical snapshot can possibly have
   an entry for them).

Implemented as two new pipeline helpers, `_match_side_team()` and
`_team_snapshot_for_event()` (`src/fpl/staging/pipeline.py`), feeding two new
optional parameters on `stage_event_live()` (`src/fpl/staging/fpl_api.py`).
Re-running against the real pulled 2026-27 data after this fix: zero null
`team_id`/`opponent_team` rows, `check --layer facts` clean, and
`fpl predict` produces 667 rows with a fully non-null
`predicted_total_points_fpl` column — the original goal (real, non-null
recommendations for the in-progress season) confirmed working end-to-end.

## Explicitly out of scope

- Retraining or changing the feature set — the diagnostic shows no design
  gap here, so there's nothing to retrain around.
- Any non-FPL third-party detailed-stats provider — not needed given the
  diagnostic; the ~15 missing columns were never load-bearing for any
  season this project already trains on.
