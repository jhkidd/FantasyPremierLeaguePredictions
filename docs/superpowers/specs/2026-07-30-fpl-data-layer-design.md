# FPL Data Layer — Ingestion & Feature Store

**Date:** 2026-07-30
**Status:** Approved design, ready for implementation planning
**Scope:** Subsystem 1 of 6. Ingestion and feature store only.

---

## 1. Context

The wider project builds a system that helps pick a winning Fantasy Premier League team: models that project player points, an optimiser that selects squads and transfers, and a phone-accessible app for making decisions before each gameweek deadline.

That system decomposes into six subsystems:

1. **Ingestion** — pull data from external sources
2. **Feature store** — turn raw data into point-in-time-correct model inputs
3. **Models** — project expected points per player per fixture
4. **Optimiser** — select squad, transfers, captain, bench order, chip timing
5. **App** — a GitHub Pages site for reviewing and adjusting decisions
6. **Evaluation** — walk-forward backtesting and in-season drift monitoring

**This spec covers subsystems 1 and 2 only.** The others get their own spec → plan → implementation cycles. Where this design fixes a contract another subsystem consumes, that contract is stated explicitly here.

### Deployment context

Decisions made during design that constrain this subsystem:

- **Public GitHub repository.** Data and code are world-readable. This is acceptable — the project relies entirely on public information. It also grants unlimited GitHub Actions minutes and free GitHub Pages hosting.
- **Models are trained locally, pre-season and ad hoc. Actions only performs inference.** Actions loads a committed model artefact and calls `predict`; it never fits.
- **The app is a static GitHub Pages site.** Nothing server-side runs at request time. Actions precomputes the recommended answers; the browser can additionally re-solve alternatives client-side.
- **Data is stored as Parquet committed to the repository**, under `data/`, written by Actions and read locally after a `git pull`.

---

## 2. Goals and non-goals

### Goals

- Provide a complete, correct, reproducible record of every input needed to project FPL points.
- Guarantee point-in-time correctness so models cannot train on information unavailable at the deadline.
- Support both local training and unattended Actions inference through **one shared feature implementation**.
- Survive an FPL API schema change loudly rather than silently.
- Remain operable from a phone: failures must notify, and data freshness must be visible.

### Non-goals

- Model design, feature selection, and hyperparameters (subsystem 3).
- Squad optimisation and chip strategy (subsystem 4).
- App UI (subsystem 5).
- Real-time in-match live scoring. The daily/pre-deadline cadence is sufficient.
- Paid data sources. Everything here is free.

---

## 3. Key research findings that shaped this design

These were verified during design and are load-bearing. They are recorded because they are non-obvious and change over time.

| Finding | Consequence |
|---|---|
| `vaastav/Fantasy-Premier-League` **stopped weekly updates** after 2024/25 — it now publishes 3 times per season. | The archive is usable for historical backfill only. All in-season data must come from the FPL API directly. |
| **FBref lost its Opta data partnership in early 2026.** Advanced and defensive stats have been removed. | FBref is not a viable source. The FPL API is now effectively the only free source of clearances, blocks, interceptions, tackles and recoveries. |
| FPL exposes `clearances_blocks_interceptions`, `tackles`, `recoveries` and `defensive_contribution` **only from 2025/26 onwards**. | Only **one season** (~21k player-fixture rows) of training data exists under the defensive-contribution rules. This is a hard constraint on subsystem 3. |
| FiveThirtyEight SPI has not been updated since 2023. | Use Club Elo for team strength instead. |
| FPL's `ep_next`, `form` and `xP` fields are **overwritten in place**, and public archives capture them post-match. | They look like excellent pre-match features but are leakage. Dropped from archive imports; usable only from our own timestamped snapshots. |
| FPL prices change nightly at approximately 01:30–02:30 UK time. | The daily snapshot must run after that window. |
| From 2026/27, FPL locks a gameweek's points at 09:00 UK the day after its last match. | Facts for a gameweek are only final after that lock. |
| Fixture counts are not stable more than ~3 gameweeks ahead; doubles and blanks are confirmed 2–6 weeks out. | The features layer must express fixture count as uncertain beyond a short horizon. |

---

## 4. Architecture

A layered pipeline, equivalent to a medallion architecture, with a deliberately **virtual** feature layer.

```
SOURCES ──► raw/ ──► staged/ ──► facts/ ──► features (library, not stored)
```

| Layer | Contents | Mutability |
|---|---|---|
| `raw/` | Exactly what each source returned, gzipped. Partitioned by source / season / `as_of` date. | Append-only, never edited |
| `staged/` | Typed, renamed, deduplicated tables. One module per source. All source-specific quirks are quarantined here. | Rebuildable from `raw/` |
| `facts/` | Canonical **one row per (player, fixture)**. Component stats are the source of truth; FPL points are *derived*. | Rebuildable from `staged/` |
| features | **Not stored.** A pure library: `build(as_of, horizon)`. | Computed on demand |

Every transform is idempotent and independently re-runnable. Any layer can be rebuilt from the one before it, and the whole pipeline can be replayed from `raw/` alone.

### Why features are a library rather than a table

This is what makes "train locally, serve in Actions" safe. There is exactly one implementation of every feature, and both callers invoke it identically, so train/serve skew is structurally impossible rather than merely avoided by discipline. It also makes changing a feature free — there is no materialised table to migrate or backfill.

### Why points are derived rather than ingested

`scoring/rules_2026_27.py` takes component stats and returns an itemised points breakdown. FPL's own `total_points` is retained **only as a reconciliation check**. This gives three things:

1. Historical seasons scored under older rules remain usable — we re-derive their points under current rules wherever the underlying stats exist.
2. Future rule changes are a new module, not a migration.
3. Comparing our derived points against FPL's actual points is the single strongest available test that we have understood the rules correctly.

---

## 5. Repository layout

### Code

```
src/fpl/
  cli.py            typer entrypoint, one command per stage
  config.py         seasons, paths, source toggles

  storage/          path conventions, parquet io, partitioning, atomic writes
  sources/          one connector per source + shared protocol
                      base.py  fpl_api.py  vaastav.py
                      understat.py  clubelo.py  footballdata.py
  staging/          raw -> staged, one module per source
  identity/         crosswalk build, resolve, validate
  scoring/          versioned rules: rules_2025_26.py, rules_2026_27.py
  facts/            staged -> canonical player_fixture facts
  features/         the as_of library (shared by training and inference)
  quality/          schema and expectation checks

  training/         fit / evaluate / walk-forward backtest
                      imported by notebooks, never by Actions
  inference/        load artefact, validate schema, predict
                      imported by Actions, never fits
  registry/         model artefact registry: resolve active.json,
                      load artefacts, validate feature schema
                      (written by training, read by inference)

notebooks/          exploration only. Never imported. Outputs stripped on commit.
tests/
.github/workflows/
```

### Data

```
data/
  raw/
    fpl/bootstrap_static/season=2026-27/as_of=2026-08-01/data.json.gz
    fpl/fixtures/season=2026-27/as_of=.../
    fpl/event_live/season=2026-27/event=1/as_of=.../
    fpl/element_summary/season=2026-27/as_of=.../
    vaastav/ understat/ clubelo/ footballdata/

  staged/
    players/ teams/ fixtures/
    player_fixture_stats/
    price_snapshots/ availability_snapshots/

  facts/
    player_fixture/season=2026-27/part.parquet
    team_fixture/
    points/rules=2026-27/

  crosswalk/
    players_fpl_understat.csv     reviewed and committed
    teams.csv

  predictions/season=2026-27/as_of=.../     append-only, also read by the app
  monitoring/                               predicted vs realised
  status.json                               freshness and active model versions

models/
  active.json                     the pointer Actions reads
  minutes/2026-08-01-a3f9/{model.joblib, metadata.json}
  goals_assists/ clean_sheet/ bonus/ defensive_contribution/
```

---

## 6. Components

| Component | Responsibility | Contract |
|---|---|---|
| `sources/` | Fetch bytes from one external source. No parsing, no logic. | `Connector.fetch(as_of, **params) -> RawArtifact`. Every artifact carries url, HTTP status, sha256, `fetched_at`, connector version. |
| `storage/` | Own every path and partition convention — the single place that knows the layout. | Writes are **content-addressed**: if the sha256 matches the previous snapshot, skip the write. Writes are atomic (temp file, then rename). |
| `staging/` | Raw → typed, renamed, deduplicated tables. | Declared polars schema per table. An unknown incoming column is a warning; a missing expected column is a failure. |
| `identity/` | Resolve FPL ↔ Understat ↔ football-data identities across seasons. | The committed CSV is the source of truth. Any player with minutes > 0 must map or be explicitly marked `no_match`. Otherwise: hard fail. |
| `scoring/` | Turn component stats into FPL points under a named ruleset. | `rules.points(row) -> PointsBreakdown` — itemised, not a single number, so every point is auditable. |
| `facts/` | Assemble the canonical grain and attach derived points. | Primary key `(season, fixture_id, player_id)`, enforced. Blanks are absent rows; doubles are two rows. |
| `features/` | Compute model inputs at a point in time. | `build(as_of, horizon) -> DataFrame`. Each feature registers the fact tables and time window it reads, so leakage is statically checkable. |
| `quality/` | Assert data sanity before anything downstream trusts it. | Runs as a gate between every layer. Failures block the commit. |
| `training/` | Fit, evaluate, backtest. Local only. | Writes model artefacts. Never runs in CI. |
| `inference/` | Load artefact, validate feature schema, predict. | Read-only on `models/`. No fitting, no hyperparameters, no randomness. |

### Data sources in scope

**Tier 1 — required**

- **FPL API** (`fantasy.premierleague.com/api/`): `bootstrap-static`, `fixtures`, `event/{gw}/live`, `element-summary/{id}`. The only source of FPL prices, ownership, availability news and defensive-contribution stats. Undocumented and unversioned; no auth needed for the endpoints used. Politeness: 2–5s between requests.
- **`vaastav/Fantasy-Premier-League`**: historical backfill for 2016/17–2025/26. Backfill only.

**Tier 2 — included**

- **Understat** (via `understatapi`): shot-level xG/xA, EPL from 2014/15. No defensive stats.
- **Club Elo** (`api.clubelo.com`): free REST team strength ratings, continuously updated.
- **football-data.co.uk**: results and bookmaker odds, free CSV.

**Tier 3 — deferred**, behind the same connector interface: scraped predicted lineups (Fantasy Football Scout, Drafthound), paid odds APIs.

### Identity resolution

FPL player IDs, Understat player IDs and football-data team names do not agree, and FPL IDs are reassigned between seasons.

Approach: bootstrap from vaastav's per-season `id_dict.csv`, generate additional candidates by fuzzy matching, but **commit a reviewed CSV as the source of truth**. Ingestion hard-fails on any player who recorded minutes and has no mapping and no explicit `no_match` marker. Team mapping is a small hand-maintained file — there are only 20 teams.

The failure mode being designed against is a silently mismatched player producing plausible but wrong predictions, which is far more expensive than a build that stops.

---

## 7. Point-in-time semantics

Two kinds of time exist in the facts layer, and `as_of` filters both.

- **Match facts** carry `kickoff_time`. Visible only where `kickoff_time < as_of`. Note this is kickoff, not full-time — a feature must not observe a match that had merely started.
- **Snapshot facts** (price, ownership, injury news, availability) carry `as_of_ts`. Visible only where `as_of_ts <= as_of`, taking the latest such row per player.

`features.build(as_of, horizon)` applies both filters. The same call is made by local training (swept across historical deadlines) and by the Actions inference job.

**Excluded fields.** `ep_next`, `form`, `xP` and any other FPL field that is overwritten in place are dropped from archive imports at the staging layer. They are usable only when sourced from our own timestamped snapshots.

---

## 8. Operating cadence

| Workflow | Schedule | Behaviour |
|---|---|---|
| `daily-snapshot` | 03:30 UTC daily | Runs after FPL's nightly price run. Pulls `bootstrap-static` and `fixtures` — two requests. Captures prices, ownership, injury news, status. |
| `post-gameweek` | 09:30 UTC daily | FPL finalises a gameweek at 09:00 UK the day after its last match. Checks whether a gameweek just locked; if so pulls `event/{gw}/live` (all players, one request), rebuilds facts, and reconciles derived points against FPL's. Once models exist it also appends predicted-vs-realised to `monitoring/`; until then that step is inert. Exits in seconds otherwise. |
| `pre-deadline` | Hourly, Thu–Sun | Reads the real deadline from `events` and no-ops unless within the window. Refreshes availability. Once models exist it runs inference and publishes predictions; until then it publishes `status.json` only. Cron cannot express "90 minutes before a moving kickoff", so the job decides. |
| `weekly-context` | Mondays | Understat, Club Elo, football-data.co.uk. Slower-moving sources. |
| `backfill` | Manual dispatch | Cold start and repair. Polite 3s spacing. A full `element-summary` sweep of ~700 players takes roughly 35 minutes, which is why routine jobs never perform one. |

All jobs share a `concurrency` group and rebase before pushing, so two runs cannot race on the same commit. The repository is public, so Actions minutes are unlimited.

### CLI surface

Actions is a scheduler, not a second implementation. Every command below is idempotent, safe to re-run, and does exactly the same thing on a laptop as in CI.

```
fpl ingest <source> --season 2026-27 [--endpoint ...]   # source -> raw/
fpl stage  <source> --season 2026-27                    # raw/   -> staged/
fpl facts           --season 2026-27 --rules 2026-27    # staged -> facts/
fpl crosswalk refresh --season 2026-27                  # propose new mappings
fpl crosswalk validate                                  # fail on unmapped players
fpl check                                               # all quality gates
fpl features --as-of 2026-08-14T11:30Z --horizon 5      # debug / inspect only
                                                        # prints a summary; writes
                                                        # only to a scratch path
                                                        # outside data/
fpl backfill --from 2016-17 --to 2025-26                # one-off cold start
```

---

## 9. Modelling and experimentation seam

**Scope note.** This section fixes the *layout and contracts* of the boundary between local training and Actions inference, because the data layer's design depends on them. Implementing `training/`, `inference/` and the models themselves belongs to subsystem 3.

Local code trains; Actions serves. The handoff is a committed pointer file.

- **`models/active.json`** names the active version per model. Swapping a model mid-season is a one-line commit — reviewable as a diff, revertable in seconds.
- **Feature-schema contract.** Each artefact's `metadata.json` records the exact ordered feature list, a hash of the feature registry it was trained against, the scoring rules version, training `as_of` range, evaluation metrics, and the git SHA. At inference, Actions rebuilds features and asserts the schema matches — **hard fail, not a warning**. Without this guard, editing a feature and forgetting to retrain yields a model silently reading the wrong columns and producing plausible nonsense.
- **Predictions are archived append-only** to `data/predictions/season=.../as_of=.../`, stamped with the producing model version. Drift cannot be measured against predictions that were not kept, and they cannot be reconstructed later because their inputs were point-in-time. This is the same artefact the Pages app reads, so archival and serving are one thing.
- **Monitoring.** `post-gameweek` joins archived predictions to realised points and appends per-model error, calibration, and error by position and price bracket to `data/monitoring/`.
- **One pinned environment.** A joblib artefact is only loadable by a compatible library stack, so local and Actions share a lockfile and Python version (`uv` with `uv.lock`). CI asserts the lockfile is current. Exporting to ONNX would break the coupling but is not worth the cost until the coupling actually bites.

**Backtesting** is walk-forward: step through historical deadlines, call `features.build(as_of)`, predict, score against what actually happened. It is the identical code path to the live run, which is precisely why its output is worth believing.

---

## 10. Error handling

Governing rule: **fail loudly, never silently degrade.** A missing day of data is visible and recoverable. Fabricated or quietly-stale data is neither, and it poisons every model trained afterwards.

| Failure | Response | Rationale |
|---|---|---|
| Network blip / 5xx | Retry with exponential backoff and jitter, then abandon the run | Tomorrow's run recovers it; snapshots are append-only, so a gap is just a gap |
| Rate limited / throttled | Back off and abandon cleanly | Politeness matters on an undocumented API we depend on entirely |
| Source schema changed | **Hard fail.** Nothing downstream runs | FPL adds and removes fields between seasons. Guessing at a changed schema produces a season of subtly wrong data |
| Unmapped player identity | **Hard fail** if they recorded minutes | A silently dropped player is invisible; a failed build is not |
| Quality gate fails | Block the commit | Bad data never reaches `main`, so `main` is always trustworthy |
| Partial write / job killed | Temp file then atomic rename; layers advance only on success | A half-written parquet that looks complete is the worst outcome |

### Observability

- **A failed workflow opens a GitHub Issue** and reuses it rather than spamming. GitHub emails and push-notifies. A cron job failing silently for three weeks is worse than no cron job — and the operator is frequently away from a laptop.
- **`status.json`** carries last-successful-run timestamps per source, active model versions, and the gameweek the predictions target. The app displays freshness prominently.
- **Stale predictions are labelled, not hidden.** If `pre-deadline` cannot get fresh data, it republishes the previous prediction *marked degraded with its true age*. Being told "this is 4 days old" allows a decision; being shown a confident stale number does not.

---

## 11. Testing

| Test | What it proves |
|---|---|
| **Points reconciliation** | Derived points equal FPL's `total_points` for every completed player-fixture in 2025/26, zero tolerance. Any mismatch is a rules bug or a data bug. This is the only real proof that scoring — including defensive-contribution thresholds and the 2026/27 BPS changes — is understood correctly. |
| **Leakage test** | Build features for a past deadline twice: once from the full archive, once from an archive truncated at that instant. Assert identical. Any feature that peeks at the future fails. |
| **Scoring golden cases** | Hand-written cases from the rules already in `docs/`: goalkeeper scores; defender on exactly 10 CBIT versus 9; midfielder on 12 CBIRT; penalty save; red card with goals conceded after it; clean sheet with a 59th-minute substitution. |
| **Connector tests** | Replay recorded HTTP responses. No network in CI — fast and deterministic. |
| **Live schema canary** | Scheduled separately from CI. Hits the real APIs and diffs their shape against staging's expectations, opening an issue on drift. This is how an upstream change surfaces in August rather than November. |
| **Invariants** | Facts primary key unique; minutes in [0, 120]; a double gameweek's rows sum to the gameweek total; a blank produces zero rows rather than a null row. |
| **Determinism** | Two runs over identical raw input produce byte-identical parquet, which makes every other diff meaningful. |

Tooling is kept proportionate: `pytest`, `ruff`, type hints on public interfaces, `uv` for the locked environment. CI runs the test suite plus the reconciliation and leakage suites on every push.

---

## 12. Storage footprint

Projected growth, which determines whether committing data to Git remains appropriate.

| Data | Cadence | Each | Per year |
|---|---|---|---|
| Volatile player fields (price, news, ownership; ~20 columns × ~700 rows) | Daily | ~40 KB | ~15 MB |
| Full `bootstrap-static` snapshot | Weekly | ~250 KB | ~13 MB |
| `event/{gw}/live` per-player gameweek stats | Per gameweek | ~120 KB | ~5 MB |
| Fixtures, teams, Understat, odds, Elo | Weekly | — | ~15 MB |

Roughly **45–50 MB per year**, plus a one-off ~50 MB backfill of 2016/17–2025/26. Ten seasons in, that is approximately **0.5 GB**.

GitHub's limits: 50 MiB per-file warning, 100 MiB hard block, repositories ideally under 1 GB and strongly recommended under 5 GB. Nothing in the Acceptable Use Policy prohibits datasets in repositories; the constraints are repository health and excessive bandwidth. Dataset repositories are an established pattern.

Two properties keep this safe rather than lucky: append-only means each commit adds a new small file rather than rewriting a large one, so there is no delta bloat; and raw payloads are stored gzipped and content-addressed, so sources that genuinely did not change between pulls — fixtures, Club Elo, football-data between match rounds — are never rewritten. `bootstrap-static` does change most nights, since ownership always shifts, which is why the daily staged snapshot keeps only the volatile column subset.

**Escape hatch.** If the footprint ever outgrows Git, the `storage/` module is the only component that knows about paths, making a move to Cloudflare R2 (10 GB free, no egress charges, no inactivity expiry) a contained change.

---

## 13. Contracts consumed by other subsystems

| Consumer | Contract |
|---|---|
| Models (3) | `features.build(as_of, horizon) -> DataFrame`; facts at `(season, fixture_id, player_id)` grain; `scoring.rules.points(row) -> PointsBreakdown` |
| Optimiser (4) | `data/predictions/season=.../as_of=.../` — per-player-per-fixture projections stamped with model version |
| App (5) | `data/predictions/`, `data/monitoring/`, `status.json` — all small, static, browser-fetchable |
| Evaluation (6) | Append-only `data/predictions/` joined to `facts/`; walk-forward via `features.build` |

---

## 14. Suggested implementation phases

This subsystem is large enough that a single undifferentiated plan would be unwieldy. The phases below are sequenced so that each ends somewhere useful and testable.

1. **Skeleton and storage.** `uv` project, `config.py`, `storage/` with partitioning, atomic and content-addressed writes, `cli.py` shell, CI running `pytest` and `ruff`.
2. **FPL API connector and raw ingestion.** `sources/base.py`, `sources/fpl_api.py`, `fpl ingest`, recorded-response tests. Ends with real snapshots on disk.
3. **Staging and quality gates.** Typed schemas, `fpl stage`, `quality/` gates between layers.
4. **Scoring rules and facts.** `scoring/rules_2026_27.py` with golden cases, `facts/` assembly, `fpl facts`. **Ends with the points reconciliation test passing against 2025/26** — the single most important milestone in this subsystem, because it proves the rules are understood.
5. **Historical backfill and identity.** vaastav connector, `identity/` crosswalk build and validation, `fpl backfill`. Ends with ten seasons of facts.
6. **Tier 2 sources.** Understat, Club Elo, football-data.co.uk through the same connector interface.
7. **Feature library.** `features/` with the registry, `as_of` filtering, and the leakage test.
8. **Automation.** The five workflows, failure-to-issue notification, `status.json`, live schema canary.

Phases 1–4 are the critical path; nothing downstream is trustworthy until reconciliation passes.

---

## 15. Open questions deferred to later specs

- Which features the store should actually compute. This spec fixes the mechanism and the contract, not the feature list; that belongs with model design, where features can be justified against measured performance.
- Model families and how to handle having only one season of defensive-contribution data.
- Whether the app's client-side optimiser is a WASM MILP solver or a simpler heuristic search.
- Whether personal squad state is read live from the FPL API in the browser or committed to the repository.
