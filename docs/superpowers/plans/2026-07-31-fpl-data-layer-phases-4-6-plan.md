# Implementation Plan — Data Layer Phases 4–6

**Date:** 2026-07-31
**Spec:** [`../specs/2026-07-30-fpl-data-layer-design.md`](../specs/2026-07-30-fpl-data-layer-design.md)
**Predecessor:** [`2026-07-30-fpl-data-layer-phases-1-3-plan.md`](2026-07-30-fpl-data-layer-phases-1-3-plan.md)
**Covers:** phases 4, 5 and 6 in full. Phases 7–9 unchanged.

---

## Where we are going in

Phases 1–3 are complete, live and scheduled. 247 tests pass. But the repository holds **bronze only** — four daily snapshots of 2026/27 pre-season, 358 KB — and **zero training data**. No staged tables, no facts, no scoring rules, no models.

Phases 4–6 exist to change exactly one thing: **turn an archive into a trustworthy training set.** They end with ten seasons of canonical player-fixture facts whose derived points reconcile against FPL's published points at zero tolerance. Until that test passes, nothing downstream is worth building, because a model trained on subtly wrong points is worse than no model — it is confidently wrong and there is no signal that tells you so.

**Nothing here is date-critical.** The 21 August deadline belongs entirely to phases 1–3, which are done. Historical data does not expire. The one hard rule is that **the GW1 rehearsal checklist in the phases 1–3 plan takes precedence on 21 August** — if this work is mid-flight then, it pauses.

---

## What the archive probe changed

Before planning I probed the vaastav archive directly rather than trusting the spec's characterisation of it. Eight findings, all verified against live data. **Two of them contradict the spec, and one materially changes what is modellable.**

### Finding 1 — three extra seasons of defensive-contribution data

Spec §4 states the defensive-contribution inputs are "2025/26 only — first exposed by the FPL API that season". **That is wrong.** The pre-2019 FPL API exposed a much richer stat set that was removed in 2019/20 and only partly restored in 2025/26. Counts of rows carrying a non-zero value, among players with 60+ minutes:

| Season | CBI | tackles | recoveries |
|---|---|---|---|
| 2016/17 | 6,609 | 3,814 | 7,816 |
| 2017/18 | 6,560 | 3,353 | 7,846 |
| 2018/19 | 6,462 | 2,810 | 7,789 |

Densely populated, not a sparse remnant. **Defensive-contribution training data goes from one season to four.** Spec §4's conclusion — "we accept one season of defensive-contribution training data" — and the entire rejected-backfill analysis that led to it were reasoning from a false premise. The third-party vendors (Sofascore, FotMob, Wyscout, StatsBomb) were rejected on licensing and definition-drift grounds; none of that applies to FPL's own historical fields.

### Finding 2 — bonus is partially derivable for 2016–19

Spec §4 lists bonus as "**Not derivable** — **no season**". Also wrong. Those three seasons publish most of the BPS input table:

| Available 2016–19 | Still missing |
|---|---|
| `attempted_passes`, `completed_passes` (→ pass-completion bands) | shots on target |
| `key_passes` (→ chance created), `big_chances_created`, `big_chances_missed` | saves inside the box |
| `open_play_crosses`, `dribbles`, `tackles` | saves from a big chance |
| `tackled`, `fouls`, `offside`, `penalties_conceded` | goalline clearances |
| `errors_leading_to_goal`, `errors_leading_to_goal_attempt` | fouls **won** |
| `clearances_blocks_interceptions`, `recoveries`, `winning_goals`, `target_missed` | |

So bonus remains **not exactly derivable** — the spec's practical conclusion survives. What changes is that we can now *measure* how much of BPS is reproducible, against three seasons where FPL's actual `bps` total is published alongside most of its inputs. That converts "model bonus blind from proxies" into "model bonus against a measured residual". Phase 6 delivers that measurement as a report.

**Honest caveat:** there is no overlap season between 2018/19 and 2025/26, so we cannot prove Opta's definitions of CBI, tackles and recoveries did not drift across the seven-year gap. This is the same objection §4 raised against third-party vendors. It is weaker here — these are FPL's own fields from FPL's own supplier, not a different vendor's ontology — but it is not zero, and the plan treats it as an explicit open risk (see Risks, R1).

### Finding 3 — `code` is a stable cross-season player key

Spec §6 says "FPL IDs are reassigned between seasons" and proposes fuzzy matching plus a hand-reviewed CSV. The first half is true; the second is unnecessary. Every season's `players_raw.csv` carries a `code` field, and it is stable.

Of 270 players present in both 2016/17 and 2019/20:

- **269 had a different `id`** — ids are indeed reassigned annually.
- **All 270 kept the same `code`.**
- 9 names disagreed, and **all 9 are the same player re-spelled**: `Muhamed Besic`→`Muhamed Bešić`, `Cuco Martina`→`Rhu-endly Martina`, `Matthew James`→`Matty James`, `Borja Bastón`→`Borja González Tomás`. **Zero collisions.**

**Cross-season FPL identity is a join, not a matching problem.** The hand-reviewed CSV and fuzzy matching are still needed — but only for cross-*source* identity (FPL ↔ Understat ↔ football-data), which is phase 7's data. That work leaves phase 6.

### Finding 4 — `id_dict.csv` exists for only two seasons

Spec §6 proposes bootstrapping identity "from vaastav's per-season `id_dict.csv`". That file exists for **2021/22 and 2022/23 only**. The approach was unworkable regardless of Finding 3. Also absent: `teams.csv` for 2016/17–2018/19, and `fixtures.csv` for 2016/17–2017/18.

### Finding 5 — seven distinct schema eras

`gws/merged_gw.csv` is not one schema. Verified column counts and deltas across all ten seasons:

| Era | Seasons | Cols | Encoding | Quoted | `name` format | Change from previous |
|---|---|---|---|---|---|---|
| **E1** | 2016/17, 2017/18 | 56 | cp1252 | yes | `Aaron_Cresswell` | — (baseline: the rich set) |
| **E2** | 2018/19 | 56 | cp1252 | **no** | `Aaron_Cresswell_402` | quoting and name format change only |
| **E3** | 2019/20 | **33** | utf-8 | no | `Aaron_Cresswell_376` | **−23 cols**: the entire rich set removed |
| **E4** | 2020/21, 2021/22 | 36 | utf-8 | no | `Aaron Connolly` | `+position +team +xP` |
| **E5** | 2022/23, 2023/24 | 41 | utf-8 | no | `Nathan Redmond` | `+expected_{goals,assists,goal_involvements,goals_conceded} +starts` |
| **E6** | 2024/25 | 49 | utf-8 | no | `Alex Scott` | `+mng_*` (7 cols) `+modified` |
| **E7** | 2025/26 | 46 | utf-8 | no | `Reinildo Mandava` | `−mng_*`, `+clearances_blocks_interceptions +tackles +recoveries +defensive_contribution` |

Three distinct name formats, two encodings, two quoting conventions, and a 23-column cliff at 2019/20.

### Finding 6 — manager assets pollute 2024/25

In 2024/25 FPL sold managers as squad assets. In the archive they appear as **`position == 'AM'`** — not `MNG` as the column names imply. 322 rows, 21 distinct people (Pep Guardiola, Thomas Frank, Fabian Hürzeler…), **all with `minutes == 0` and non-zero `total_points`** scored entirely through the `mng_*` columns.

Left in, these are 322 rows of "players" who scored points without playing. They would break points reconciliation at zero tolerance and would teach a minutes model that scoring without appearing is possible. **They must be excluded at staging, with the excluded count logged and asserted.**

### Finding 7 — encoding and quoting drift

E1 and E2 are **not valid UTF-8** — they are cp1252, carrying accented player names. Naive UTF-8 decoding either throws or silently mojibakes (`Bešić` → `BeÅ¡iÄ‡`), and mojibake is the worse failure because it survives into a committed parquet. Encoding is **declared per era, never sniffed.**

### Finding 8 — `defensive_contribution` is a count, and `GK`/`GKP` coexist

`defensive_contribution` in 2025/26 is the **count** of qualifying actions, not the 2 points awarded. Verified: Omar Alderete, CBI 22 + tackles 1 → `defensive_contribution` 23; Wesley Fofana, CBI 18 + tackles 4 → 22. For defenders it excludes recoveries, exactly as the rules state. This makes it a free check on our own threshold logic rather than something to trust.

Separately, **2021/22 labels goalkeepers two ways in one season**: `GK` on 2,809 rows and `GKP` on 101. Normalise, and assert the normalised set is exactly `{GK, DEF, MID, FWD}`.

---

## Spec amendments

These land as a single commit at the **start of phase 4**, before any code, so no implementation work is done against text known to be wrong.

| Spec location | Change |
|---|---|
| §4 tier table | Shallow tier becomes 2016/17–2018/19 **and** 2025/26. Add a fourth tier for BPS inputs (2016/17–2018/19). |
| §4 "Bonus is the sharp problem" | Rewrite: bonus is not exactly derivable in any season, but is *measurable* against 2016–19. |
| §4 "We accept one season" | Replace with four seasons, and record the definition-drift caveat (R1) explicitly. |
| §4 rejected-backfill paragraph | Keep — the third-party rejection still stands — but note it no longer implies a one-season ceiling. |
| §6 Identity resolution | Cross-season FPL identity is a join on `code`. Fuzzy matching and the reviewed CSV are scoped to cross-source identity only. Delete the `id_dict.csv` bootstrap (exists for 2 of 10 seasons). |
| §11 reconciliation row | Ten seasons under three rulesets, not one season. |
| §15 phases 4–6 | Reflect the resequencing below. |

---

## Resequencing

The spec's order cannot work. **Phase 5's exit criterion is reconciliation against completed 2025/26 player-fixtures, and the only source of those is vaastav — which the spec puts in phase 6, after phase 5.** The FPL API serves the current season only; we proved prior-season data is gone when `entry/1/event/38/picks/` returned 404.

Resolution: the **vaastav connector and a 2025/26-only slice move into phase 4**. The full ten-season backfill, identity and the BPS report stay in phase 6.

```
Phase 4  staging framework + quality gates + FPL API staging
         + vaastav connector + 2025/26 slice
   ↓
Phase 5  scoring rules (×3) + facts assembly + reconciliation on 2025/26
   ↓
Phase 6  remaining six eras + identity on `code` + teams crosswalk
         + reconciliation on all ten seasons + BPS residual report
```

This keeps reconciliation as early as possible, which is the whole point of having it — it is the test that tells us whether everything before it was real.

---

## Locked technical decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Staging engine | **polars, eager** | Already a dependency. Datasets are ~30k rows/season; laziness buys nothing and costs debuggability. |
| Schema declaration | **Explicit `polars.Schema` per (source, table, era)** | Spec §6: unknown column → warning, missing expected column → failure. Both need a declared expectation to compare against. |
| Era resolution | **Explicit `season → era` map, never sniffed** | Sniffing turns an upstream change into a silent reinterpretation. A new season must fail until someone classifies it. |
| Encoding | **Declared per era** | Finding 7. Sniffing mojibake is undetectable downstream. |
| Bonus | **Observed passthrough in all three rulesets; never derived** | Finding 2. Reconciliation must be exact, so bonus enters as FPL's published value. Derivation is a modelling problem, not a scoring one. |
| Availability mask | **One boolean per component *group*, not per column** | Spec §4 says per component; that is ~30 boolean columns of which only 4 patterns ever occur. Groups carry identical information at a tenth the width. |
| Manager rows | **Excluded at staging, count logged and asserted** | Finding 6. Silent exclusion is how you later discover a 322-row hole and cannot explain it. |
| Archive market data | Staged to `price_snapshots` with `as_of_ts` **flagged as approximate** | `value`/`selected`/`transfers_*` in `merged_gw` have no true timestamp — only a gameweek. Approximating with the deadline is fine; pretending it is exact is not. |
| Rules modules | **`rules_legacy` (2016/17–2024/25), `rules_2025_26`, `rules_2026_27`** | The only points-affecting change in ten seasons is defensive contribution in 2025/26. The 2026/27 changes are BPS-only, so its points arithmetic equals 2025/26's — but it gets its own module so a future divergence is a new file, not an edit. |
| Clean sheets | **Derived from `minutes` and `goals_conceded`, not read from the `clean_sheets` flag** | Reading FPL's flag makes reconciliation circular. Deriving it, then gating against the flag, is a real test. |
| Facts partitioning | `facts/player_fixture/season=…/part.parquet`; points under `rules=…` | Spec §5, unchanged. |

---

## Phase 4 — Staging and quality gates

**Goal:** raw bytes become typed, gated, rebuildable tables — and 2025/26 is on disk ready to be scored.

### 4.0 Spec amendments

Land the table above. **Done when:** committed, and no statement in §4/§6/§11 contradicts a verified finding.

### 4.1 Staging framework — `src/fpl/staging/base.py`

```python
@dataclass(frozen=True)
class ColumnSpec:
    name: str  # our name
    source_name: str  # theirs
    dtype: pl.DataType
    required: bool = True
    group: str = "core"  # availability group


@dataclass(frozen=True)
class TableSpec:
    table: str  # e.g. "player_fixture_stats"
    columns: tuple[ColumnSpec, ...]
    key: tuple[str, ...]
    encoding: str = "utf-8"
    drop: frozenset[str] = ...  # ep_next, form, xP — spec §7
```

`stage_frame(raw, spec) -> StagedFrame` applies: decode → parse → rename → cast → drop → validate. Returns the frame plus a `StagingReport` (rows in/out, unknown columns seen, rows excluded and why).

**Unknown column → warning on the report. Missing `required` column → `SchemaError`.** Spec §6 and §10.

**Tests:** rename and cast; unknown column warns and is dropped; missing required column raises; drop-list removes `ep_next`/`form`/`xP` even when present; cp1252 payload decodes to `Bešić` not `BeÅ¡iÄ‡`; a declared-utf8 spec given cp1252 bytes raises rather than mojibaking.

### 4.2 Quality gate framework — `src/fpl/quality/`

```python
@dataclass(frozen=True)
class Violation:
    gate: str; detail: str; rows: int; sample: list[dict]

def run_gates(frame, gates) -> list[Violation]
```

A gate is a named pure function `frame -> list[Violation]`. Gates carry a `severity` of `block` or `warn`. Any `block` violation → exit **13** (`QUALITY_GATE_FAILED`), and in CI that blocks the commit (spec §10).

Generic gates: `unique_key`, `not_null`, `in_range`, `non_negative`, `enum_values`, `referential`.

**Tests:** each gate on a passing and a failing frame; violation carries a sample so the failure is diagnosable from the log alone; `run_gates` aggregates rather than short-circuiting, so one run reports every problem.

### 4.3 FPL API staging — `src/fpl/staging/fpl_api.py`

Stages what is already on disk from phases 1–3:

| Raw | Staged table | Key |
|---|---|---|
| `bootstrap-static` → `elements` | `players` | `(season, player_id)` |
| `bootstrap-static` → `teams` | `teams` | `(season, team_id)` |
| `bootstrap-static` → `events` | `events` | `(season, event)` |
| `bootstrap-static` → volatile subset | `price_snapshots` | `(season, player_id, as_of_ts)` |
| `bootstrap-static` → news/status | `availability_snapshots` | `(season, player_id, as_of_ts)` |
| `fixtures` | `fixtures` | `(season, fixture_id)` |
| `entry` | `entry_snapshots` | `(season, entry_id, as_of_ts)` |
| `entry_picks` chunks | `manager_picks` | `(season, cohort, event, entry_id, player_id)` |

`as_of_ts` comes from the raw partition name, which is why phase 1 made it the only mutable-free source of truth. `manager_picks` carries the `contaminated` flag through from chunk metadata — it is a property of the data and must not be lost at the staging boundary.

**Tests:** each table against a recorded payload; `as_of_ts` recovered from the partition; several `as_of` partitions stack into one snapshot table without duplicating; a contaminated chunk stays flagged; a cohort is never pooled with another.

### 4.4 CLI — `fpl stage` and `fpl check`

```
fpl stage <source> --season 2026-27 [--table ...]
fpl check [--season ...] [--layer staged|facts]
```

Idempotent; rebuilds from raw every time (spec §4 — staged is derived, never authored). Writes through `write_parquet` with `sort_by=key`, so an unchanged rebuild is a byte-identical file and therefore an empty Git diff.

**Tests:** staging twice produces byte-identical output; `fpl check` exits 13 on a seeded violation and 0 on clean data; `--table` restricts scope.

### 4.5 vaastav connector — `src/fpl/sources/vaastav.py`

**One tarball, not hundreds of file requests** (spec §13 — the GitHub API budget). Fetches `https://github.com/vaastav/Fantasy-Premier-League/archive/refs/heads/master.tar.gz`, extracts only the paths asked for, and writes each extracted file as its own content-addressed raw artifact under `raw/vaastav/<table>/season=…/`.

The tarball is ~200 MB and is **never committed** — it lands in a scratch directory outside `data/` and is deleted. Only the extracted per-season CSVs are stored, gzipped and content-addressed, so re-running the backfill after an upstream correction rewrites only the seasons that actually changed.

Files per season: `gws/merged_gw.csv`, `players_raw.csv`, and `teams.csv` / `fixtures.csv` where they exist (Finding 4 — absent for the earliest seasons; **absence is expected and must not fail**).

**Tests:** extraction from a small fixture tarball; a season whose `teams.csv` is missing stages without error; a second run with unchanged bytes writes nothing; requesting an unknown season fails loudly.

### 4.6 vaastav staging, era E7 only — `src/fpl/staging/vaastav.py`

Build the era machinery, then use it for **2025/26 alone**. The other six eras are phase 6; standing them up here would delay reconciliation for no gain.

```python
ERA_BY_SEASON: dict[Season, str] = {Season(2025): "E7", ...}
SPECS: dict[str, TableSpec] = {"E7": TableSpec(...)}
```

A season with no era entry raises. Deliberate: a new upstream season must be classified by a person.

Normalisations applied here and nowhere else:

- position `GKP` → `GK`; assert the result is exactly `{GK, DEF, MID, FWD}` (Finding 8)
- exclude `position == 'AM'`, log and assert the count (Finding 6 — inert for E7, but the rule belongs with the spec, not the era)
- `name` parsed per era (Finding 5); **never used as a key** — `element` + `code` are
- `was_home` string `"True"`/`"False"` → boolean
- `kickoff_time` → UTC-aware datetime
- drop `xP` (spec §7), and `value`/`selected`/`transfers_*` routed to `price_snapshots`, not to stats

**Tests:** the E7 spec against a trimmed real 2025/26 fixture; `GKP` normalises; an `AM` row is excluded and counted; a cp1252 fixture round-trips (proving the machinery before E1 needs it); `defensive_contribution == cbi + tackles` for defenders and `== cbi + tackles + recoveries` for others, on real rows.

### 4.7 Phase 4 exit criteria

- `fpl stage fpl --season 2026-27` and `fpl stage vaastav --season 2025-26` both succeed from a clean tree.
- `staged/player_fixture_stats/season=2025-26/` holds ~29,757 rows carrying CBI, tackles, recoveries and `defensive_contribution`.
- `fpl check` is clean; a seeded violation exits 13.
- Re-running any stage command produces an empty Git diff.
- Spec §4/§6/§11 amended.

---

## Phase 5 — Scoring rules and facts

**Goal:** the single most important milestone in the subsystem — **prove we understand FPL's scoring.**

### 5.1 Scoring framework — `src/fpl/scoring/base.py`

```python
@dataclass(frozen=True)
class PointsBreakdown:
    appearance: int
    goals: int
    assists: int
    clean_sheet: int
    goals_conceded: int
    saves: int
    penalties_saved: int
    penalties_missed: int
    yellow_cards: int
    red_cards: int
    own_goals: int
    defensive_contribution: int
    bonus: int

    @property
    def total(self) -> int: ...


class Rules(Protocol):
    name: str

    def points(self, row: PlayerFixture) -> PointsBreakdown: ...
```

**Itemised, never a single number** (spec §6). When reconciliation fails on 30,000 rows, the breakdown is what turns "we are 2 points out" into "our clean-sheet term is wrong for substitutes".

`points()` is pure, takes one row, and has no knowledge of parquet, paths or seasons.

### 5.2 Three rulesets

`rules_legacy.py` (2016/17–2024/25), `rules_2025_26.py`, `rules_2026_27.py`.

Shared arithmetic lives in `base.py`; each module declares only what differs. Concretely:

- **legacy** — no defensive-contribution term.
- **2025/26** — adds DC: `+2` if `cbi + tackles >= 10` for `DEF`, or `cbi + tackles + recoveries >= 12` for `MID`/`FWD`. Never stacks. GKs never qualify.
- **2026/27** — points arithmetic identical to 2025/26. Its own module because the 2026/27 changes we know of are BPS-only, and a shared module would make a future divergence an edit to a file two seasons depend on.

Terms common to all three: appearance 1 / 2 at 60'; goals 10/6/5/4 by position; assist 3; clean sheet 4/4/1/0 by position, **derived** as `minutes >= 60 and goals_conceded == 0`; `−floor(gc/2)` for GK and DEF; `+floor(saves/3)`; penalty save 5; penalty miss −2; yellow −1; red −3; own goal −2; bonus passed through as observed.

**Do not model BPS in this phase.** Bonus enters as FPL's published value.

### 5.3 Golden cases

Hand-written from `docs/Fantasy Premier League Scoring.md`, asserted term by term rather than on the total — a total can be right for two compensating wrong reasons.

Minimum set, taken from spec §11 plus the boundaries the rules actually turn on:

| Case | Proves |
|---|---|
| Defender, 10 CBIT | DC threshold inclusive |
| Defender, 9 CBIT | threshold exclusive |
| Defender, 20 CBIT | DC does not stack |
| Midfielder, 12 CBIRT / 11 CBIRT | the higher threshold, and that recoveries count for MID |
| Defender, 12 CBI+tackles+**recoveries** but 9 CBIT | recoveries do **not** count for DEF |
| Goalkeeper, high CBIT | GKs never earn DC |
| GK, 3 / 5 / 6 saves | integer division, not rounding |
| Player subbed at 59' / exactly 60' | appearance boundary |
| Clean sheet with a 59th-minute substitution | the spec's named case |
| Red card, goals conceded after it | deductions continue |
| Penalty save + penalty miss same match | both terms independent |
| Own goal by a goalkeeper who also kept a clean sheet | terms do not interact |
| Manager row (`minutes == 0`, points > 0) | **rejected**, not scored |

**Every case is asserted against all three rulesets**, with the DC cases expected to differ under `legacy`. That is what stops a rule being silently added to the wrong era.

### 5.4 Facts assembly — `src/fpl/facts/player_fixture.py`

Primary key `(season, fixture_id, player_id)`, enforced. Blanks are absent rows; doubles are two rows (spec §6).

Column groups:

| Group | Columns |
|---|---|
| Keys | `season`, `fixture_id`, `player_id`, **`player_code`** (stable — Finding 3) |
| Context | `team_id`, `opponent_team_id`, `was_home`, `kickoff_time`, `event`, `position`, `minutes`, `starts` |
| Core components | `goals_scored`, `assists`, `goals_conceded`, `own_goals`, `penalties_saved`, `penalties_missed`, `yellow_cards`, `red_cards`, `saves` |
| Defensive | `cbi`, `tackles`, `recoveries`, `defensive_contribution` |
| BPS inputs | `attempted_passes`, `completed_passes`, `key_passes`, `big_chances_created`, `big_chances_missed`, `open_play_crosses`, `dribbles`, `tackled`, `fouls`, `offside`, `target_missed`, `errors_leading_to_goal`, `errors_leading_to_goal_attempt`, `penalties_conceded`, `winning_goals` |
| Expected | `expected_goals`, `expected_assists`, `expected_goal_involvements`, `expected_goals_conceded` |
| Observed FPL output | `total_points_fpl`, `bonus_fpl`, `bps_fpl` — **reconciliation targets, never features** |
| Availability mask | `obs_defensive`, `obs_bps_inputs`, `obs_expected`, `obs_starts` |

**On the mask.** Spec §4 asks for a boolean per component. In practice only four independent patterns exist across seven eras, so four group booleans carry identical information at a tenth the width. They are stored **per row**, not per season, so a consumer never needs the era map — the frame is self-describing. An absent component is written **null with mask false**, never zero: the whole point is that ten seasons of missing tackles must not read as ten seasons of zero tackles.

The `_fpl` suffix on observed outputs is deliberate. A feature-builder that reaches for `total_points` gets a `KeyError`, not a leak.

### 5.5 CLI — `fpl facts`

```
fpl facts --season 2025-26 --rules 2025-26
```

Writes `facts/player_fixture/season=…/part.parquet` and `facts/points/rules=…/season=…/part.parquet`. Rebuildable, deterministic, atomic.

### 5.6 Points reconciliation — the milestone

`tests/test_reconciliation.py`, run in CI:

> For every completed player-fixture in 2025/26, `rules_2025_26.points(row).total == row.total_points_fpl`. **Zero tolerance.**

On failure the report groups mismatches **by term and by position** and prints the ten worst rows. A bare count is useless; "all 412 mismatches are defenders whose DC term is 2 too low" is a fix.

Fact-layer invariants, gated (spec §11): key unique; `minutes` in [0, 120]; a double gameweek's rows sum to that gameweek's total; a blank yields zero rows, not a null row; `defensive_contribution` equals its definition wherever observed; `obs_*` constant within a season; no row with `minutes == 0` and `total_points_fpl > 0`.

### 5.7 Phase 5 exit criteria

- **Reconciliation passes on 2025/26 at zero tolerance.**
- All golden cases pass against all three rulesets.
- `facts/player_fixture/season=2025-26/` written, key-unique, invariants clean.
- Rebuilding facts twice gives an empty Git diff.

---

## Phase 6 — Historical backfill, identity, and the BPS report

**Goal:** ten seasons of facts, all reconciling, on a stable player key.

### 6.1 The remaining six eras

Add `TableSpec`s for E1–E6 (Finding 5). Per-era work, in ascending order of nastiness:

| Era | Work |
|---|---|
| E5, E6 | Closest to E7. E6 additionally excludes the `AM` manager rows (Finding 6) — 322 rows, count asserted. |
| E4 | 2021/22 `GKP`→`GK` (Finding 8). |
| E3 | The 33-column floor. Everything outside core is null with mask false. `name` carries a trailing id to strip. |
| E2 | cp1252, unquoted, `First_Last_id`. **Rich column set returns.** |
| E1 | cp1252, quoted, `First_Last`, no `position` column (join `players_raw.element_type`), no `teams.csv`, no `fixtures.csv`. |

For E1–E3, `position` is absent from `merged_gw` and comes from `players_raw.element_type` joined on `element`. For E1–E2 the team id→name map is absent and is hand-written into the teams crosswalk (60 rows, 20 teams × 3 seasons).

`fixtures.csv` is absent for E1. Fixture-level context (`opponent_team`, `was_home`, `team_h_score`, `team_a_score`) is present in `merged_gw` itself, so `team_fixture` is synthesised from it — and cross-checked against `fixtures.csv` for every season that has one, which is what makes trusting the synthesis for the two that don't defensible.

**Tests:** one trimmed real fixture per era, committed; a per-era row-count assertion against the true archive counts; an era-map gap raises.

### 6.2 Identity on `code` — `src/fpl/identity/players.py`

Not fuzzy matching (Finding 3). Build `crosswalk/players_fpl.csv` from every season's `players_raw.csv`:

```
player_code, first_seen_season, last_seen_season, canonical_name, name_variants, seasons_seen
```

`canonical_name` is the **most recent** spelling — later spellings restore accents that earlier ones stripped (`Besic`→`Bešić`).

`fpl crosswalk validate` hard-fails when:
- a `code` maps to two players whose names are not plausible variants of one another (surface for review; do not auto-resolve);
- any row in `player_fixture` with `minutes > 0` has an `element` that resolves to no `code`.

Spec §10: an unmapped player who recorded minutes is a hard fail, because a silently dropped player is invisible while a failed build is not.

**Tests:** a code appearing in ten seasons collapses to one row; a genuinely reused code (constructed) fails validation rather than merging two people; the 9 known name variants from Finding 3 are accepted as variants.

### 6.3 Teams crosswalk — `crosswalk/teams.csv`

Hand-maintained, ~200 rows. `(season, team_id) → team_code, canonical_name`. `team_code` is stable across seasons; `team_id` is not. Seeded from `teams.csv` where present (2019/20 onward) and hand-written for 2016/17–2018/19 from `players_raw.team` / `team_code`. Small, reviewable, committed.

### 6.4 `fpl backfill`

```
fpl backfill --from 2016-17 --to 2025-26 [--skip-fetch]
```

One tarball fetch, then per season: ingest → stage → facts → check. Fails loudly on the first season that will not reconcile rather than pressing on — a partial backfill that looks complete is the failure mode §10 exists to prevent. `--skip-fetch` re-derives from raw already on disk, which is the common case during development.

### 6.5 Reconciliation across ten seasons

Extend the phase 5 test to every season, each under its era's ruleset — `legacy` for 2016/17–2024/25, `rules_2025_26` for 2025/26. Zero tolerance throughout.

**This will surface real archive defects,** and that is the point. Expect trouble at: the 2019/20 COVID suspension and its restart fixtures; 2022/23's World Cup break; postponed and rearranged fixtures appearing in two gameweeks; the 2024/25 manager rows if 6.1 missed any. Each is triaged as **rules bug, staging bug, or upstream archive defect**. Only the third may be quarantined, and only via an explicit dated exclusion list carrying a reason — never by loosening the tolerance.

### 6.6 BPS residual report — `notebooks/bps_reproducibility.ipynb`

**Bounded: a measurement, not a model.** Deliberately a notebook, so it cannot be imported by anything and cannot quietly become a dependency.

For 2016/17–2018/19, compute the BPS terms we *can* observe from Finding 2's available list, and compare the sum to FPL's published `bps`. Report:

- distribution of `bps_observed − bps_fpl`, overall and by position;
- how much of the residual the missing terms plausibly explain (shots on target, saves in the box, saves from a big chance, goalline clearances, fouls won);
- **how often the residual changes the top-3 bonus ordering within a match** — the only thing that actually matters, since bonus is awarded on rank, not on level.

Output is a short written finding appended to the design doc, feeding the modelling spec. **No fitted model. No new dependency. No production code path.**

### 6.7 Phase 6 exit criteria

- Ten seasons in `facts/player_fixture/`, all reconciling at zero tolerance.
- `crosswalk/players_fpl.csv` and `crosswalk/teams.csv` committed and validating.
- `fpl backfill --from 2016-17 --to 2025-26` runs clean from an empty tree.
- Defensive-contribution components observed for **four** seasons, mask correct.
- BPS residual report written and its conclusion recorded in the spec.
- Total repository size still comfortably inside the §12 budget (expected +50–70 MB).

---

## Cross-cutting

### CLI surface after phase 6

```
fpl stage <source> --season …  [--table …]
fpl facts --season … --rules …
fpl check [--season …] [--layer staged|facts]
fpl crosswalk validate
fpl backfill --from … --to … [--skip-fetch]
```

Unchanged from spec §8 except `fpl crosswalk refresh`, which is **deferred to phase 7** — there is nothing to refresh against until a second source exists (Finding 3).

### Testing

Existing conventions hold: no network in CI, recorded fixtures, `filterwarnings = ["error"]`, fixed date literals never season-relative, ruff line length 100.

New fixtures are **trimmed real payloads** — a few hundred rows per era, every column retained — following the `scripts/record_fixtures.py` pattern from phase 2. Extend it to `scripts/record_archive_fixtures.py`.

CI additions: reconciliation (2025/26 in phase 5, all ten in phase 6), fact invariants, staging determinism.

**The leakage test stays in phase 8.** It tests `features.build`, which does not exist yet.

### Risks

| # | Risk | Mitigation |
|---|---|---|
| **R1** | **Opta definitions for CBI / tackles / recoveries drifted between 2018/19 and 2025/26.** No overlap season exists to prove otherwise. | Mask by *era*, not just by presence, so a model can be trained with and without the early seasons and the difference measured. Record the caveat in §4. Compare per-90 distributions by position across the gap as a smell test — a large shift is evidence of drift even though a small one proves nothing. |
| R2 | Reconciliation fails on an early season for an unfixable upstream reason | Dated, reasoned exclusion list. Never loosen tolerance. A season that cannot reconcile is reported as untrusted rather than quietly included. |
| R3 | vaastav rewrites history upstream | Raw layer is content-addressed, so a changed file is visible as a real diff. Never fetch straight into staged. |
| R4 | The 200 MB tarball exhausts runner disk | Extract to scratch outside `data/`, delete after. Backfill is manual-dispatch only, never scheduled. |
| R5 | Repo growth from ten seasons of parquet | Measure after phase 6. §12's escape hatch — `storage/` is the only module that knows paths — is already in place. |
| R6 | Phase 6 collides with the 21 August GW1 rehearsal | The rehearsal wins. Phases 4–6 have no deadline. |

### Deliberately out of scope

Understat, Club Elo, football-data, cup fixtures (phase 7). The feature library and the leakage test (phase 8). `post-gameweek`, `pre-deadline`, `weekly-context`, `status.json`, the schema canary (phase 9). Any fitted model.

---

## Sequenced task list

| # | Task | Phase | Depends on |
|---|---|---|---|
| 1 | Spec amendments §4/§6/§11/§15 | 4 | — |
| 2 | `staging/base.py` — `ColumnSpec`, `TableSpec`, `stage_frame` | 4 | 1 |
| 3 | `quality/` gate framework + generic gates | 4 | — |
| 4 | `staging/fpl_api.py` — 8 tables | 4 | 2, 3 |
| 5 | `fpl stage` / `fpl check` | 4 | 4 |
| 6 | `sources/vaastav.py` — tarball connector | 4 | — |
| 7 | `staging/vaastav.py` + era map + E7 spec | 4 | 2, 6 |
| 8 | `scoring/base.py` — `PointsBreakdown`, `Rules` | 5 | — |
| 9 | `rules_legacy` / `rules_2025_26` / `rules_2026_27` | 5 | 8 |
| 10 | Golden cases ×3 rulesets | 5 | 9 |
| 11 | `facts/player_fixture.py` + availability mask | 5 | 7, 9 |
| 12 | `fpl facts` | 5 | 11 |
| 13 | **Reconciliation on 2025/26 — zero tolerance** | 5 | 12 |
| 14 | Fact invariant gates | 5 | 11 |
| 15 | Era specs E1–E6 | 6 | 7, 13 |
| 16 | `identity/players.py` on `code` + validate | 6 | 15 |
| 17 | `crosswalk/teams.csv` | 6 | 15 |
| 18 | `fpl backfill` | 6 | 15, 16, 17 |
| 19 | **Reconciliation on all ten seasons** | 6 | 18 |
| 20 | BPS residual report | 6 | 19 |
| 21 | Record findings back into the spec | 6 | 20 |

Task 13 is the gate. Nothing after it is worth doing if it does not pass, and nothing before it is trustworthy until it does.
