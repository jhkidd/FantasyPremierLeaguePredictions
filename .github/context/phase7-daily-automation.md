# Task: Wire Phase 7 Tier 2 sources into the daily automation

## Section 1 — Q&A (Verbatim)

**Context (assistant's investigation, preceding the Q&A):** Phase 7's four Tier 2 connectors
(Club Elo, openfootball, football-data.co.uk, Understat) and `facts/team_fixture` were built and
manually run once on 2026-08-03/04, then never wired into any scheduled GitHub Actions workflow.
`.github/workflows/daily-snapshot.yml` only runs `fpl ingest fpl` / `fpl stage fpl`. Checked live
against the repo on 2026-08-24 (mid-GW1 weekend of the 2026-27 season):
- Club Elo: one 2026-27 snapshot, dated 2026-08-03 (3 weeks stale).
- football-data.co.uk and openfootball: zero 2026-27 data at all.
- `facts/team_fixture` for 2026-27 (760 rows) built 2026-08-04, not rebuilt since.
- Plan task 15 (verify Club Elo reachable from a real GitHub Actions runner before scheduling it)
  was never done — likely why it was never added to the daily workflow.

**Q:** Extend daily-snapshot.yml, or a separate new workflow file for Tier 2 sources?
**A:** Extend daily-snapshot.yml (one daily run, one commit sequence, matches existing pattern).

**Q:** Should the daily workflow also rebuild facts (`fpl facts --season <current>`) daily?
**A:** Yes, as the last step.

**Q:** How to handle the plan's task 15 (Club Elo runner-connectivity verification)?
**A:** Run a one-off throwaway probe workflow first (curl Club Elo from an Actions runner), confirm
it works, then wire Club Elo into the daily job permanently — matches the plan's original R1
design; do not just add it straight to the daily job untested.

**Q:** If one Tier 2 source fails on a given day, should the rest still get committed?
**A:** Yes — each source's ingest/stage step should be independent (`continue-on-error: true`) so
one source's outage never blocks the others or the core `fpl` snapshot.

**Q:** Should a Tier 2 failure raise/update a GitHub issue, like `capture-ownership.yml` does?
**A:** Yes.

**Q:** One shared issue for all Tier 2 sources, or one per source?
**A:** One shared issue, reused across any Tier 2 failure (simplest, mirrors
`capture-ownership.yml`'s single-issue-reuse pattern). Label: `tier2-capture`.

**Q:** What `--limit` for Understat's per-match backfill (`--endpoint match_backfill`) in the daily
job (one HTTP request per match, ~2s spacing, resumable/chunked)?
**A:** 50 matches/day (~2 min) — comfortably covers a full gameweek (~10 matches) plus catch-up
room if a day is missed.

**Q:** Should this task also do a one-time manual catch-up backfill for 2026-27 right now (since
footballdata/openfootball have zero 2026-27 data and Club Elo is 3 weeks stale), in addition to
wiring up the daily automation?
**A:** Yes — catch up now, then set up daily automation going forward.

**Q:** What `timeout-minutes` should the job have (currently 20)?
**A:** 30 minutes.

**Q:** Should `fpl check --layer facts` run after the daily facts rebuild, failing the step (not
just warning) on a violation?
**A:** Yes.

**Q:** Production code, or a prototype/ideation pass?
**A:** Production — same rigor as the rest of `daily-snapshot.yml` / `capture-ownership.yml`.

## Section 1b — Probe result and follow-up decision (2026-08-24)

The throwaway probe (`.github/workflows/probe-clubelo-runner.yml`) was run twice on a real
`ubuntu-latest` GitHub Actions runner:
- Run 1 (unbounded): hung for the full 5-minute job timeout, cancelled, zero output.
- Run 2 (bounded, with DNS + HTTP + HTTPS diagnostics): DNS resolved fine
  (`api.clubelo.com` → `37.128.134.74`). **HTTP (port 80)**: TCP connect succeeded
  ("Connected to api.clubelo.com ... port 80") but then 0 bytes were received in 15s
  (`curl: (28) Operation timed out ... with 0 bytes received`) — the connection is accepted and
  then silently blackholed, not a fast error. **HTTPS (port 443)**: the TCP connect itself timed
  out within 8s — the port is not reachable at all.

**Conclusion: Club Elo is not reliably reachable from a GitHub Actions runner**, on either scheme —
worse than the sandbox's earlier 502 (Finding A), and confirming Risk R1's stated fallback.

**Q:** Proceed with the other 3 sources + facts wiring, leaving Club Elo manual/local-only, or
pause everything until a Club Elo workaround is found?
**A:** Proceed without Club Elo in the daily workflow (openfootball + football-data.co.uk +
Understat + facts rebuild/check only). Club Elo stays a manual/local-only capture for now,
revisited later.

**Revised scope for the rest of this task:**
- The daily workflow gains `ingest`/`stage` steps for `openfootball`, `footballdata`, and
  `understat` only — **no** `ingest_clubelo`/`stage_clubelo` steps, and no Club Elo entry in the
  failure-issue check list.
- The one-off manual catch-up backfill still includes Club Elo (steps in Section 2 below), run
  from this local machine/session, not from Actions — local network egress to Club Elo already
  works (the existing 2026-08-03 capture and this session's local `uv run fpl ingest clubelo`
  calls are unaffected by the Actions-runner-specific block found above).
- The probe workflow file has already been deleted (its job — answering R1 — is done).

## Locked design decisions (derived from the above + codebase investigation)

- **CLI surface is already complete** — no new Python code needed, only workflow YAML + one-off
  shell commands run locally for the catch-up. Confirmed commands:
  - `fpl ingest clubelo` (defaults to today's date; T-1 forward-building series)
  - `fpl backfill-elo --from <season> --to <season>` (historical dates derived from already-staged
    `facts/player_fixture` kickoffs — used only for the one-off catch-up, not the daily job, since
    the daily job's `ingest clubelo` already builds the forward series one day at a time)
  - `fpl ingest openfootball`, `fpl ingest footballdata`
  - `fpl ingest understat` (league_data/season-aggregate; default endpoint)
  - `fpl ingest understat --endpoint match_backfill --limit N` (per-match; omit `--limit` for the
    one-off catch-up so it clears everything played so far this season)
  - `fpl stage clubelo|openfootball|footballdata|understat` — each is a no-op-but-successful
    "no capture on disk" result if that source's ingest failed or hasn't run yet, so **staging
    steps do not need to be gated on their ingest step's outcome** — just give every new step
    `continue-on-error: true` and let staging's existing graceful-skip behaviour do the rest.
  - `fpl facts` (defaults to current season; already builds both `player_fixture` and
    `team_fixture` in one call, per the 2026-08-04 update to the phase 7 plan)
  - `fpl check --layer facts` (defaults to current season)
  - None of the new steps pass `--season` explicitly in the daily workflow (mirrors the existing
    `fpl stage fpl` step, which also relies on the `CURRENT_SEASON` default). The one-off catch-up
    commands run locally *do* pass `--season 2026-27` explicitly for clarity/safety.
- **Failure semantics**: every new ingest/stage step gets `id:` + `continue-on-error: true` except
  the final facts rebuild and `fpl check --layer facts`, which run normally (a real failure there
  stops the job, since a broken facts table must never be silently committed). This means a Tier 2
  source outage will **not** turn the whole Actions run red — the shared GitHub issue is the
  intended failure signal instead. A `facts_check` failure **will** turn the run red (as normal),
  and will also raise/update the same shared issue.
- **Commit boundaries preserved**: `git add data/raw/` after all ingest steps (existing "Commit
  raw" step, unchanged in structure, now naturally picks up all 5 sources); a new "Commit staged"
  addition for the 4 new `stage_*` calls right after the existing `fpl stage fpl` staging step,
  same commit as today; a **new** "Commit facts" step (`git add data/facts/`) after
  `fpl check --layer facts` passes.
- **Permissions**: add `issues: write` to `daily-snapshot.yml` (currently `contents: write` only),
  matching `capture-ownership.yml`.
- **Probe workflow**: new throwaway file `.github/workflows/probe-clubelo-runner.yml`,
  `workflow_dispatch`-only trigger, one step that `curl`s `http://api.clubelo.com/<today's date>`
  from the runner and prints the HTTP status + a body snippet. Run once via `gh workflow run`,
  inspect the result, then **delete the file** once Club Elo is confirmed reachable (or stop and
  report back if it is not, per the plan's original R1 fallback: Club Elo becomes manual-backfill
  only, not scheduled).

## Section 2 — Step-by-Step Implementation Plan

1. **Create and run the Club Elo runner-connectivity probe.** Add
   `.github/workflows/probe-clubelo-runner.yml` (workflow_dispatch only, one step:
   `curl -sS -o /tmp/body.txt -w "HTTP %{http_code}\n" "http://api.clubelo.com/$(date -u +%F)"` then
   `head -c 300 /tmp/body.txt`). Commit and push it, then trigger it with
   `gh workflow run probe-clubelo-runner.yml` and `gh run watch` (or poll) to confirm HTTP 200 with
   real CSV content. If it fails (502 or otherwise), stop and report back to the user instead of
   proceeding to step 2 onward for Club Elo specifically.

2. **Delete the probe workflow** once Club Elo connectivity is confirmed (`git rm
   .github/workflows/probe-clubelo-runner.yml`), commit.

3. **Edit `.github/workflows/daily-snapshot.yml`**:
   - Add `issues: write` under `permissions`.
   - Change `timeout-minutes: 20` to `timeout-minutes: 30`.
   - After the existing "Ingest" step (`fpl ingest fpl`), add 6 new ingest steps, each with a
     distinct `id:` and `continue-on-error: true`: `ingest_clubelo` (`fpl ingest clubelo`),
     `ingest_openfootball` (`fpl ingest openfootball`), `ingest_footballdata`
     (`fpl ingest footballdata`), `ingest_understat_league` (`fpl ingest understat`),
     `ingest_understat_matches` (`fpl ingest understat --endpoint match_backfill --limit 50`).
   - Leave the existing "Commit raw" step where it is (right after these new ingest steps, so it
     naturally commits everything ingested so far) — no content change needed, it already
     `git add data/raw/` unconditionally.
   - After the existing "Stage" step (`fpl stage fpl`), add 4 new stage steps, each with a
     distinct `id:` and `continue-on-error: true`: `stage_clubelo` (`fpl stage clubelo`),
     `stage_openfootball` (`fpl stage openfootball`), `stage_footballdata`
     (`fpl stage footballdata`), `stage_understat` (`fpl stage understat`).
   - Leave the existing "Commit staged" step where it is, right after these new stage steps — no
     content change needed.
   - Add a new "Rebuild facts" step (id `facts_rebuild`, no continue-on-error):
     `uv run fpl --verbose facts`.
   - Add a new "Check facts" step (id `facts_check`, no continue-on-error):
     `uv run fpl --verbose check --layer facts`.
   - Add a new "Commit facts" step (mirroring "Commit staged"'s shape exactly):
     `git add data/facts/`, skip if nothing staged, else commit as
     `data: facts snapshot $(date -u +%Y-%m-%dT%H:%MZ)`, rebase-pull, push.
   - Add a final "Raise an issue on Tier 2 capture failure" step, `if: always()`, reading each of
     the 10 new steps' `outcome` (`ingest_clubelo`, `stage_clubelo`, `ingest_openfootball`,
     `stage_openfootball`, `ingest_footballdata`, `stage_footballdata`,
     `ingest_understat_league`, `ingest_understat_matches`, `stage_understat`, `facts_check`) via
     `env:`, checking each for `"failure"` in bash, and — if any failed — using `gh issue list
     --label tier2-capture` / `gh issue comment` / `gh issue create` exactly mirroring
     `capture-ownership.yml`'s existing "Raise an issue on failure" step structure, with label
     `tier2-capture` and a body listing which step(s) failed and a link to the run.

4. **Run `uv run ruff check .` / `ruff format --check .`** — no Python changed, but run anyway to
   confirm the working tree's Python is untouched/clean (cheap sanity check, no code should have
   changed in this task).

5. **One-off manual catch-up backfill for the 2026-27 season**, run locally in this session (not
   in CI), in this order, checking each command's output before proceeding:
   - `uv run fpl --verbose ingest clubelo --season 2026-27` (today's rating)
   - `uv run fpl --verbose backfill-elo --from 2026-27 --to 2026-27` (fills the T-1 historical
     dates that GW1's already-played fixtures need)
   - `uv run fpl --verbose ingest openfootball --season 2026-27`
   - `uv run fpl --verbose ingest footballdata --season 2026-27`
   - `uv run fpl --verbose ingest understat --season 2026-27` (league/season aggregate)
   - `uv run fpl --verbose ingest understat --season 2026-27 --endpoint match_backfill` (no
     `--limit` — one-off, clears everything played so far this season, expected to be small since
     only GW1 has been played)
   - `uv run fpl --verbose stage clubelo --season 2026-27`
   - `uv run fpl --verbose stage openfootball --season 2026-27`
   - `uv run fpl --verbose stage footballdata --season 2026-27`
   - `uv run fpl --verbose stage understat --season 2026-27`
   - `uv run fpl --verbose facts --season 2026-27`
   - `uv run fpl --verbose check --season 2026-27 --layer facts` — must pass clean before
     committing.

6. **Commit the catch-up data** in the same layer-separated style the workflows use: one commit
   for `data/raw/`, one for `data/staged/`, one for `data/facts/` (only the layers that actually
   changed), each with a `data: ...` style message consistent with the automated commits' naming,
   plus the `Co-authored-by: Copilot` trailer.

7. **Update the phase 7 plan doc**
   (`docs/superpowers/plans/2026-08-02-fpl-data-layer-phase-7-plan.md`) with a dated note under
   §7.15 exit criteria: Club Elo runner connectivity confirmed live (date + result), Tier 2 sources
   now wired into `daily-snapshot.yml`'s schedule, and the 2026-27 catch-up backfill's row counts.

8. **Update `README.md`'s Status section** only if it currently implies Phase 7 automation is
   outstanding in a way this work resolves (check current wording first; edit only if needed).

9. **Final verification**: confirm `git status` is clean, `daily-snapshot.yml` is valid YAML (e.g.
   `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/daily-snapshot.yml'))"` or
   equivalent), and — if feasible without waiting for tomorrow's schedule — trigger the workflow
   once via `gh workflow run daily-snapshot.yml -f force=true` and watch it run to completion,
   confirming the new steps behave as expected (including that a `continue-on-error` step failing,
   if any do, does not stop the job).

10. **Commit the workflow changes** (`.github/workflows/daily-snapshot.yml`, probe workflow
    deletion) in one commit, separate from the data commits in step 6, with the
    `Co-authored-by: Copilot` trailer.
