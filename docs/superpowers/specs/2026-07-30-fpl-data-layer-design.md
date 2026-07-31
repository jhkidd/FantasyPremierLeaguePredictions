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
| **FBref lost its Opta data partnership in early 2026.** Advanced and defensive stats have been removed. Sports Reference, 2026-01-20: the provider "sent us a letter terminating our access to their data feeds and requiring the deletion of their data from the site immediately". | FBref is not a viable source. The FPL API is now effectively the only free source of clearances, blocks, interceptions, tackles and recoveries. |
| FPL exposes `clearances_blocks_interceptions`, `tackles`, `recoveries` and `defensive_contribution` **only from 2025/26 onwards**. | Only **one season** (~21k player-fixture rows) of training data exists under the defensive-contribution rules. This is a hard constraint on subsystem 3. |
| FiveThirtyEight SPI has not been updated since 2023. | Use Club Elo for team strength instead. |
| FPL's `ep_next`, `form` and `xP` fields are **overwritten in place**, and public archives capture them post-match. | They look like excellent pre-match features but are leakage. Dropped from archive imports; usable only from our own timestamped snapshots. |
| FPL prices change nightly at approximately 01:30–02:30 UK time. | The daily snapshot must run after that window. |
| From 2026/27, FPL locks a gameweek's points at 09:00 UK the day after its last match. | Facts for a gameweek are only final after that lock. |
| Fixture counts are not stable more than ~3 gameweeks ahead; doubles and blanks are confirmed 2–6 weeks out. | The features layer must express fixture count as uncertain beyond a short horizon. |
| **OpenFPL** (arXiv:2508.09992, 2025) matched the commercial FPL Review Massive Data Model on hauler RMSE (5.142 vs 5.172) using **only the FPL API and Understat**. | Strong evidence that the Tier 1 + Tier 2 source set is sufficient to reach state-of-the-art on public data. Paid odds feeds are not a prerequisite. |
| Manager picks are **not retained by FPL across seasons**, and no public archive reconstructs them. | Effective ownership is the one dataset in this design that cannot be backfilled. Capture must begin in-season (see §6.1). |
| **The overall league is empty until a gameweek has been scored.** League 314 was recreated on 2026-07-23 and returns zero standings rows despite 2.28m registered entries. | There is no top 1,000 to enumerate during gameweek 1. Elite capture starts at gameweek 2; the mini-league cohort, which is enumerable from the start, covers gameweek 1 (§6.1). |
| Entry IDs are issued in registration order, and early registrants are far stronger: median 2025/26 rank 127k for IDs 1–25 against 2.25m for IDs near 400k. | A skill-based cohort can be assembled without waiting for a gameweek. `entry/{id}/history/` is immutable, so this is available at any time and is deliberately off the critical path (§6.1). |

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

### Ground truth: what history can and cannot teach

The 2026/27 season has not been played, so every model must be trained on earlier seasons scored under different rules. Whether that is legitimate depends entirely on *what* is being learned.

**Component stats are rule-invariant. Market data is not.** A defender does not make more clearances because FPL changed how it rewards them. So minutes, goals, tackles and saves observed in 2019/20 are valid training data for 2026/27, and re-deriving points from them under current rules is sound. Price, ownership, transfers in and out are the opposite: they are collective *responses* to the rules, so a 2019/20 ownership series describes behaviour under a scoring system that no longer exists. Market data is therefore only ever used within its own season.

**Three tiers of component availability.** Not every component reaches equally far back:

| Tier | Components | Depth |
|---|---|---|
| Deep | minutes, goals, assists, clean sheets, goals conceded, saves, cards, penalties, own goals | ~10 seasons |
| Shallow | clearances, blocks, interceptions, tackles, recoveries (the defensive contribution inputs) | 2025/26 only — first exposed by the FPL API that season |
| **Not derivable** | **bonus** | **no season** |

Bonus is the sharp problem. The BPS table needs passes, crosses, dribbles, big chances created, shots on target, fouls won, goalline clearances and saves-in-box — FPL publishes the BPS *total* but none of its inputs, and no free source supplies them. Bonus must therefore be **modelled from observable proxies**, not computed. Historical `bps` totals carry a known bias under 2026/27 rules (CBI now scores 1 BPS per 3 rather than per 2; being tackled no longer costs −1; a penalty save is 7 not 8), so for 2025/26 the totals can be partially corrected because CBI counts are available, and for earlier seasons they can only be used as a noisy ordinal signal.

**Consequence: never model total points directly.** Model each component and sum through the scoring rules. A single total-points model would be limited to the shallowest input it depends on; component models each train on the deepest history available to them.

**Consequence: `facts/player_fixture` carries a per-row availability mask.** A boolean column per component recording whether that component was *observed* for that row. Without it a model reads ten seasons of missing tackle data as ten seasons of zero tackles, which is not a subtle error — it is a systematic one concentrated exactly on the defenders the shallow tier exists to evaluate.

Backfilling the shallow tier was investigated and rejected. Every source with Opta-compatible definitions and per-match depth (Sofascore, FotMob, WhoScored) prohibits redistribution from a public repository, and Football DataCo database rights apply on top of the site terms. The legally clean alternatives cover one Premier League season each (Wyscout 2017/18 under CC BY 4.0; StatsBomb 2015/16), have no 2025/26 overlap to validate against, and Wyscout's "recovery" is a possession-level event rather than Opta's per-player one — so it cannot reproduce the `R` term that the midfielder and forward threshold of 12 depends on. Mislabelled rows at exactly the threshold would be worse than no backfill. **We accept one season of defensive-contribution training data.**

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
    fpl/league_standings/season=2026-27/event=1/as_of=.../
    fpl/entry/season=2026-27/as_of=.../
    fpl/entry_picks/season=2026-27/cohort=self/event=1/chunk=0000/
    fpl/entry_picks/season=2026-27/cohort=mini/event=1/chunk=0000/
    fpl/entry_picks/season=2026-27/cohort=elite/event=2/chunk=0000/
    vaastav/ understat/ clubelo/ footballdata/ cupfixtures/

  staged/
    players/ teams/ fixtures/
    player_fixture_stats/
    price_snapshots/ availability_snapshots/
    manager_picks/

  facts/
    player_fixture/season=2026-27/part.parquet
    team_fixture/
    effective_ownership/season=2026-27/
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
- **FPL API manager endpoints**: `leagues-classic/314/standings`, `entry/{id}/`, `entry/{id}/event/{gw}/picks/`. Used to derive effective ownership — see §6.1. Public and unauthenticated.
- **`vaastav/Fantasy-Premier-League`**: historical backfill for 2016/17–2025/26. Backfill only.

**Tier 2 — included**

- **Understat** (via `understatapi`): shot-level xG/xA. **All six covered leagues, not just the Premier League** — EPL, La Liga, Bundesliga, Serie A, Ligue 1, RFPL, from 2014/15. Pulling the others costs little and gives summer signings an xG/xA prior instead of making them cold-start unknowns, which matters most in exactly the early gameweeks where everyone else is guessing. No defensive stats.
- **Club Elo** (`api.clubelo.com`): free REST team strength ratings, continuously updated.
- **football-data.co.uk**: results and bookmaker odds, free CSV.
- **Non-league fixture congestion**: UEFA Champions League / Europa League / Conference League, FA Cup and EFL Cup schedules, from a free source such as openfootball or the api-football free tier. Not available from any other source in scope. Congestion is the main driver of rotation, and rotation is the main source of minutes uncertainty for the expensive assets where minutes matter most.

**Tier 3 — deferred**, behind the same connector interface: scraped predicted lineups (Fantasy Football Scout, Drafthound), set-piece and penalty-taker designations, forward-looking double/blank gameweek forecasts, paid player-level odds APIs.

### 6.1 Effective ownership — and why it cannot wait

Research into how consistently high-ranking managers actually reason established that **rank optimisation is not expected-points maximisation**. What matters is a player's *effective ownership* among rivals:

```
EO% = ownership% + captaincy% + (2 × triple-captaincy%)
```

A high-EV player that almost everyone owns and captains protects rank but cannot gain it; rank is gained by differentials, and lost by them too. Optimising raw expected points while ignoring EO produces systematic over-differentiation and unstable rank. Global ownership from `bootstrap-static` is not a substitute — it is diluted by millions of dormant teams and carries no captaincy information at all.

**EO is not a model input.** No prediction depends on who owns a player. EO enters only at the decision layer, where the question is how much rank a pick gains if it comes off against how much it loses if it does not. A gap in EO therefore degrades team selection, never forecast accuracy.

**This dataset is use-it-or-lose-it.** FPL does not retain manager picks across seasons: once 2026/27 ends, its picks are gone, and no public archive reconstructs them (verified 2026-07-30: `entry/1/event/38/picks/` returns 404 for the completed 2025/26 season). Unlike every other source in this design, effective ownership **cannot be backfilled**.

The expiry is at *season* rollover, not gameweek end. Picks for a completed gameweek remain readable for the rest of the season, so a missed week can be recovered later within the same season — but nothing survives into the next one.

### Two rival sets, and why they need different treatment

The design tracks two populations, because "who am I competing with" has two answers with different mathematics.

| | Overall rank | Mini-league |
|---|---|---|
| Opponents | ~2.3m | ~10–20 |
| Observability | Sampled — the top 1,000 stand in for the field | **Exact — every rival's squad is readable** |
| Cost per gameweek | ~1,020 requests | ~20 requests |
| Correct strategy | Maximise expected points with mild variance control | Depends on standing: copy rivals when ahead to kill variance, differentiate when behind even at a cost in expected points |

The overall case needs a statistical proxy because the field cannot be enumerated. The mini-league case needs no proxy at all — the league's standings endpoint returns every member, and each member's picks are one request. Mini-league capture is therefore both cheaper and *more* accurate than the elite sample, and it is the one that maps to the primary objective.

### Why elite capture starts at gameweek 2

**The overall league is empty until a gameweek has been scored.** Verified 2026-07-30: league 314 was recreated on 23 July and returns `standings.results: []` despite 2.28m registered entries. Ranks do not exist before a ball is kicked, so during gameweek 1 there is no top 1,000 to enumerate. Elite capture therefore begins at gameweek 2.

Gameweek 1 is skipped rather than reconstructed. It could be captured retroactively once gameweek 1 is scored, but that cohort would be selected on a single gameweek's outcome — mostly noise — and would not mean the same thing as every other week's cohort. A silently different definition in one row of a time series is worse than an honest gap.

The cost is small, and specific: picking the initial 15 is a constrained maximisation in which ownership has no place, so the opening squad is unchanged. Captaincy is the only real loss, and pre-season global ownership is an unusually good substitute there — it is normally diluted by abandoned teams, but only the engaged fraction of last season's ~12m managers has registered so far, so it currently describes active pickers. The residual cost is one missing observation in 38 when backtesting rank-optimised decisions.

An alternative cohort was investigated and deferred rather than rejected: entry IDs are issued in registration order, and early registrants are dramatically stronger, so a skill-based cohort could be assembled before any gameweek by scanning low entry IDs for prior-season rank.

| Entry ID band | Median 2025/26 rank | Finished top 10k |
|---|---|---|
| 1–25 | 127k | 2/21 |
| ~1,000 | 376k | 2/20 |
| ~20,000 | 1.13m | 1/19 |
| ~400,000 | 2.25m | 0/18 |

This is deliberately *not* on the critical path: `entry/{id}/history/` is immutable, so the scan yields the same cohort whenever it is run. It can be built at leisure and applied retroactively to any gameweek still in the season.

**Capture design.** All three cohorts use the same mechanism: resolve a set of entries, then fetch `entry/{id}/event/{gw}/picks/` for each. They differ only in how the set is resolved, in scale, and in when they start.

| Cohort | Entries resolved from | Entries | Requests per gameweek | From |
|---|---|---|---|---|
| Self | our own configured team ID | 1 | 1, instant | GW1 |
| Mini-league | discovered or configured league ID | All members | ~20, under a minute | GW1 |
| Elite | `leagues-classic/314` (Overall) | Top 1,000 | ~1,020, ~35 min at 2s | GW2 |

Each captured gameweek records which cohort it came from, so the three are never averaged together.

**Why our own squad is captured too.** It expires exactly as everyone else's does. Without it there is no record of what was actually played, so a recommendation can never be measured against the decision taken — which is the whole of evaluation (§2). One request a gameweek buys the entire ground truth for our own performance.

**The mini-league ID is discovered, not configured.** League IDs are reissued every season and do not exist until someone creates the league, so a hand-set ID guarantees a window in which the job silently captures nothing — verified 2026-07-30, when the 2025/26 join code was already dead and the entry belonged to system leagues only. Capture therefore reads `entry/{id}/`, keeping leagues a person created (`league_type: "x"`) and discarding the Overall, country and sponsor leagues everyone is placed into (`"s"`). More than one candidate is reported rather than guessed at: capturing the wrong opponents is worse than capturing none and being told. An explicit ID always wins.

Discovery is a lookup and stores nothing, and runs only when a capture would otherwise proceed — the job ticks 48 times a day, and an idle tick must write nothing at all. The valuable, time-varying parts of the entry document — bank, squad value, overall rank, all published only as a current value — are captured by the daily snapshot instead, at a cadence that suits them.

**The capture window is the whole gameweek, not the 90 minutes before kickoff.** The rules state that automatic substitutions are *processed at the end of the gameweek*, so a team's recorded starting XI and captain reflect the manager's actual decision at any point from the deadline until the gameweek's last match finishes — a window of two to three days rather than ninety minutes. Two consequences follow, and both matter:

- The job can be scheduled generously and retried repeatedly, instead of having to land inside a narrow slot with a 35-minute runtime.
- Correctness must still be *asserted*, not assumed. Each captured pick set carries an `automatic_subs` array which is empty until the gameweek completes. **A non-empty `automatic_subs` means the capture ran too late and the XI has been rewritten** — the record is marked contaminated rather than silently stored as if it were a decision.

Contamination degrades the record, it does not destroy it. `automatic_subs` names each substitution's `element_in` and `element_out`, so the original XI can be reconstructed; squad membership and the `is_captain` flag are never rewritten at all. A late capture is thus recoverable, which is why a missed gameweek is worth collecting late rather than abandoning.

The set of entries is stable across the window for the same reason: FPL's official ranks do not update live during a gameweek, so `leagues-classic/314/standings` returns the previous gameweek's confirmed ranking throughout. Capturing early and capturing late select the same 1,000 managers.

Top-1,000 is a deliberate approximation of the top-10k benchmark that community tools use: it is an order of magnitude cheaper and its bias is known and consistent, which is what matters for a feature used comparatively.

Top-1,000 is a deliberate approximation of the top-10k benchmark that community tools use: it is an order of magnitude cheaper and its bias is known and consistent, which is what matters for a feature used comparatively.

Staged output is one row per (gameweek, entry, player) with captain and vice-captain flags, from which EO per player per gameweek aggregates directly.

> **Verification status (updated 2026-07-30).** `entry/{id}/`, `entry/{id}/history/` and `leagues-classic/314/standings/` are confirmed public and unauthenticated. `entry/{id}/event/{gw}/picks/` returns 404 for a completed prior season, confirming picks are not retained across seasons; it therefore still **cannot be verified for a live gameweek** until GW1 kicks off. Live tools depend on it, so it is near-certainly public, but the connector must be exercised against a real gameweek at the first opportunity.

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
| `daily-snapshot` | 03:30 UTC daily | Runs after FPL's nightly price run. Pulls `bootstrap-static`, `fixtures` and our own `entry` — three requests. Captures prices, ownership, injury news, status, and our bank, squad value and overall rank. |
| `post-gameweek` | 09:30 UTC daily | FPL finalises a gameweek at 09:00 UK the day after its last match. Checks whether a gameweek just locked; if so pulls `event/{gw}/live` (all players, one request), rebuilds facts, and reconciles derived points against FPL's. Once models exist it also appends predicted-vs-realised to `monitoring/`; until then that step is inert. Exits in seconds otherwise. |
| `pre-deadline` | Hourly, Thu–Sun | Reads the real deadline from `events` and no-ops unless within the window. Refreshes availability. Once models exist it runs inference and publishes predictions; until then it publishes `status.json` only. Cron cannot express "90 minutes before a moving kickoff", so the job decides. |
| `capture-ownership` | Every 30 min | Fires once per gameweek, after that gameweek's deadline and before its last match finishes (§6.1). Captures our own squad and the mini-league every gameweek, and the top 1,000 overall entries from gameweek 2 — the overall league has no ranking before then. ~35 minutes at full scale, resumable. An idle tick writes nothing at all. **The one job that cannot be missed within a season** — it runs frequently, resumes partial progress, and raises an issue immediately on failure rather than waiting for the next run. |
| `weekly-context` | Mondays | Understat (all six leagues), Club Elo, football-data.co.uk, cup and European fixture schedules. Slower-moving sources. |
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
| `403 Forbidden` | **Do not retry.** Raise an issue immediately | A 403 from Cloudflare means the runner is blocked, not that the request was mistimed. Retrying into a block wastes requests and delays discovery (§13) |
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
| Top-1,000 manager picks (15 rows per entry) | Per gameweek | ~100 KB | ~4 MB |
| Fixtures, teams, Understat (six leagues), odds, Elo, cup schedules | Weekly | — | ~25 MB |

Roughly **60–65 MB per year**, plus a one-off ~50 MB backfill of 2016/17–2025/26. Ten seasons in, that is approximately **0.7 GB**.

**Measured against reality (2026-07-30, first live ingestion).** A full `bootstrap-static` snapshot is 1.33 MB of JSON compressing to **115 KB**, and `fixtures` is 118 KB compressing to **4.7 KB** — so a complete daily `daily-snapshot` commit is **~120 KB**, against the ~250 KB weekly figure the table assumed. The estimate above is conservative and stands. Two assumptions were wrong in offsetting directions: the roster is 564 players pre-season rather than ~700 (it grows through the season), while a full snapshot is stored every day rather than weekly, because content addressing already suppresses the unchanged ones and a whole-payload snapshot is simpler than a volatile-column subset.

GitHub's limits: 50 MiB per-file warning, 100 MiB hard block, repositories ideally under 1 GB and strongly recommended under 5 GB. Nothing in the Acceptable Use Policy prohibits datasets in repositories; the constraints are repository health and excessive bandwidth. Dataset repositories are an established pattern.

Two properties keep this safe rather than lucky: append-only means each commit adds a new small file rather than rewriting a large one, so there is no delta bloat; and raw payloads are stored gzipped and content-addressed, so sources that genuinely did not change between pulls — fixtures, Club Elo, football-data between match rounds — are never rewritten. `bootstrap-static` does change most nights, since ownership always shifts, which is why the daily staged snapshot keeps only the volatile column subset.

**Escape hatch.** If the footprint ever outgrows Git, the `storage/` module is the only component that knows about paths, making a move to Cloudflare R2 (10 GB free, no egress charges, no inactivity expiry) a contained change.

---

## 13. Credentials, rate limits and access risk

### Credentials

**No credentials or API keys are required for any source in scope.** Nothing needs to be created, and no repository secrets are needed beyond the `GITHUB_TOKEN` that Actions provides automatically.

| Source | Auth | Notes |
|---|---|---|
| FPL API | None | The endpoints in scope (`bootstrap-static`, `fixtures`, `event/{gw}/live`, `element-summary`) are all public. Only `my-team` and live own-squad endpoints need a session cookie, and those are out of scope. |
| Understat | None | Public pages, scraped via `understatapi`. |
| Club Elo | None | Free public REST API. **HTTP only — `https://api.clubelo.com` does not respond; use `http://`.** Acceptable here as the data is public and non-sensitive, but the connector must not silently upgrade the scheme. |
| football-data.co.uk | None | Static CSV downloads. |
| vaastav archive | None | Public GitHub repository. |

### Rate limits

Nothing in the free tiers constrains the cadence in §8.

| Limit | Value | Our usage |
|---|---|---|
| FPL API | No published limit. Community norm is 2–5s between requests. | `daily-snapshot` makes 2 requests; `post-gameweek` makes 1. Comfortably inside any plausible limit. |
| Club Elo | No documented limit. | A handful of requests weekly. |
| football-data.co.uk | Static files, no limit. | One CSV weekly. |
| GitHub API | 60/hour unauthenticated; 1,000/hour per repository with `GITHUB_TOKEN`. | Only relevant to the vaastav backfill, which must fetch a **single tarball or shallow clone** rather than hundreds of individual raw-file requests. |
| Actions minutes | Unlimited for public repositories. | Unconstrained. |
| Pages bandwidth | 100 GB/month soft limit. | Published artefacts are a few hundred KB. |

The one genuinely expensive operation is a full `element-summary` sweep of ~700 players at 3s spacing — roughly 35 minutes. This is why it is confined to manual backfill and never appears in a scheduled job. The `capture-ownership` job is comparable in cost (~1,020 requests at 2s) but runs only once per gameweek and is unavoidable, since the data expires. Both sit far inside the 6-hour Actions job limit.

### Access risk: datacenter IP blocking — tested and cleared

The FPL site sits behind Cloudflare, and there are community reports of `403 Forbidden` when the API is called from datacenter IP ranges — AWS, Azure, and by extension GitHub Actions runners — even for a single well-behaved request, because the traffic is classified as automated rather than because any limit was exceeded. This would have invalidated the "Actions pulls the data" premise, so it was tested before any other implementation work.

**Result: no block.** A throwaway workflow on an `ubuntu-latest` runner (Azure egress `172.184.210.251`, 2026-07-30) probed every source in scope:

| Probe | HTTP |
|---|---|
| `bootstrap-static`, curl's default user-agent | `200` |
| `bootstrap-static`, descriptive project user-agent | `200` |
| `bootstrap-static`, browser user-agent | `200` |
| `fixtures` | `200` |
| `event/{gw}/live` | `200` |
| Understat | `200` |
| Club Elo (`http`) | `200` |
| football-data.co.uk | `200` |

Notably the default curl user-agent was not blocked, so no header spoofing is required. `event/{gw}/live` returned an empty payload, which is correct — the 2026/27 season had not started.

This is a point-in-time result, not a guarantee: Cloudflare policy can change without notice. So the design retains its defences.

1. **Send a descriptive `User-Agent`** identifying the project and a contact address. Good practice regardless, and it makes our traffic legible if anyone ever looks.
2. **Treat `403` as a distinct failure class from `5xx`.** A 403 means blocked, not transient: raise an issue immediately rather than retrying into the block. This is an explicit entry in the failure taxonomy in §10.
3. **Fallbacks, should the policy ever change:** a self-hosted runner on a home machine keeps the entire design intact — same workflows, same CLI, just a different runner label — at the cost of that machine being on. Scheduled local runs pushing to the repository are the last resort, since they lose unattended operation.

Because §10 makes staleness visible through `status.json`, a block that develops mid-season is detectable rather than silent.

---

## 14. Contracts consumed by other subsystems

| Consumer | Contract |
|---|---|
| Models (3) | `features.build(as_of, horizon) -> DataFrame`; facts at `(season, fixture_id, player_id)` grain; `scoring.rules.points(row) -> PointsBreakdown` |
| Optimiser (4) | `data/predictions/season=.../as_of=.../` — per-player-per-fixture projections stamped with model version |
| App (5) | `data/predictions/`, `data/monitoring/`, `status.json` — all small, static, browser-fetchable |
| Evaluation (6) | Append-only `data/predictions/` joined to `facts/`; walk-forward via `features.build` |

---

## 15. Suggested implementation phases

This subsystem is large enough that a single undifferentiated plan would be unwieldy. The phases below are sequenced so that each ends somewhere useful and testable.

> **Hard date: the 2026/27 season starts on 21 August 2026.** Phases 1–3 must be live and scheduled before the gameweek 1 deadline, because effective ownership cannot be captured retrospectively (§6.1). Everything from phase 4 onwards can proceed at leisure — historical data is not going anywhere.

1. **Skeleton and storage.** `uv` project, `config.py`, `storage/` with partitioning, atomic and content-addressed writes, `cli.py` shell, CI running `pytest` and `ruff`.
2. **FPL API connector and raw ingestion.** `sources/base.py`, `sources/fpl_api.py`, `fpl ingest`, recorded-response tests, and the `daily-snapshot` workflow. Ends with real snapshots on disk and accumulating nightly. (Runner connectivity was verified during design — see §13. `daily-snapshot` was moved here from phase 3: pre-season price and ownership movement begins when the game opens in early August and is use-it-or-lose-it, so the earlier it is scheduled the more of it we keep.)
3. **Ownership capture, scheduled.** The manager endpoints and the `capture-ownership` workflow, covering both the mini-league cohort (from gameweek 1) and the elite cohort (from gameweek 2, when the overall league first has a ranking). Deliberately ahead of staging and facts: raw capture is what expires, and raw capture is enough to preserve the data. Interpreting it can wait. **Must be live before 21 August.**
4. **Staging and quality gates.** Typed schemas, `fpl stage`, `quality/` gates between layers.
5. **Scoring rules and facts.** `scoring/rules_2026_27.py` with golden cases, `facts/` assembly, `fpl facts`. **Ends with the points reconciliation test passing against 2025/26** — the single most important milestone in this subsystem, because it proves the rules are understood.
6. **Historical backfill and identity.** vaastav connector, `identity/` crosswalk build and validation, `fpl backfill`. Ends with ten seasons of facts.
7. **Tier 2 sources.** Understat (all six leagues), Club Elo, football-data.co.uk, cup and European fixture schedules, through the same connector interface.
8. **Feature library.** `features/` with the registry, `as_of` filtering, and the leakage test.
9. **Remaining automation.** `post-gameweek`, `pre-deadline` and `weekly-context` workflows, failure-to-issue notification, `status.json`, live schema canary.

Phases 1–3 are date-critical. Phases 4–5 are the correctness critical path; nothing downstream is trustworthy until reconciliation passes.

---

## 16. Open questions deferred to later specs

- Which features the store should actually compute. This spec fixes the mechanism and the contract, not the feature list; that belongs with model design, where features can be justified against measured performance.
- Model families, and how to make the most of a single season of defensive-contribution data. §4 settles that the data cannot be legally backfilled and that the availability mask keeps the gap visible; what remains open is the modelling response — pooling across positions, hierarchical priors, or proxying from minutes and position.
- Whether the app's client-side optimiser is a WASM MILP solver or a simpler heuristic search.
- Whether personal squad state is read live from the FPL API in the browser or committed to the repository.

---

## Appendix A — Prior art reviewed

Consulted during design. Recorded so later subsystems do not have to rediscover them.

| Project / work | Relevance | Licence | Link |
|---|---|---|---|
| **OpenFPL** (Groos, arXiv:2508.09992, 2025) | Position-specific ensembles trained on FPL API + Understat only. Matched commercial FPL Review on hauler RMSE. The benchmark that validates our source selection. | Open source | [arXiv](https://arxiv.org/abs/2508.09992) · [GitHub](https://github.com/daniegr/OpenFPL) |
| **AIrsenal** (Alan Turing Institute) | Full pipeline: Bayesian Poisson prediction into squad optimisation. Closest analogue to the whole system. | MIT | [GitHub](https://github.com/alan-turing-institute/AIrsenal) |
| **FPL-Optimization-Tools** (sertalpbilal) | Reference implementation of FPL as a multi-period MILP. Takes exogenous expected-points vectors — it is purely the optimiser, so it composes with our own predictions. | Apache 2.0 (personal use) | [GitHub](https://github.com/sertalpbilal/FPL-Optimization-Tools) |
| **penaltyblog** (martineastwood) | Dixon-Coles and bivariate Poisson goal models. A building block for team-level goal prediction. | MIT | [GitHub](https://github.com/martineastwood/penaltyblog) |
| **FPL Review Massive Data Model** | Commercial benchmark. Uses betting odds and predicted lineups we deliberately do not buy. | Commercial | [docs.fplreview.com](https://docs.fplreview.com/the-model/projections/massive-data-model/) |
| **LiveFPL** (Ragabolly) | The reference tool for top-10k effective ownership. Establishes that manager picks are publicly readable in-season. | Service | [livefpl.net](https://www.livefpl.net/top10k) |

Strategic principles from consistently high-ranking managers (Joshua Bull, Mark Sutherns, Ben Crellin, Simon March) were also reviewed. They bear on the optimiser rather than the data layer, with one exception that shaped this spec: **rank optimisation requires effective ownership**, which is why §6.1 exists and why its capture is date-critical.
