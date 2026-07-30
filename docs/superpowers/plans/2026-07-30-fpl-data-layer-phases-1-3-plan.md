# Implementation Plan — Data Layer Phases 1–3

**Date:** 2026-07-30
**Spec:** [`docs/superpowers/specs/2026-07-30-fpl-data-layer-design.md`](../specs/2026-07-30-fpl-data-layer-design.md)
**Covers:** Phases 1–3 in full detail. Phases 4–9 sketched in §Later phases.

---

## Why these three phases, and why now

The 2026/27 season starts on **21 August 2026** — 22 days from this plan's date. Phases 1–3 exist to have one thing working before that deadline: **capturing the top-1,000 managers' picks from gameweek 1 onwards.**

That is the only dataset in the entire design that cannot be obtained later. Everything else — ten seasons of history, xG, odds, Elo — will still be sitting there in October. Manager picks will not: FPL discards them at season end and no public archive reconstructs them (spec §6.1). Miss gameweek 1 and the 2026/27 effective-ownership series has a hole in it forever, which means no EO features and no honest backtest of rank-optimised decisions for that season.

So phases 1–3 are deliberately narrow. They build the minimum that makes capture trustworthy — storage that cannot half-write, a connector that fails loudly, and a scheduled job that resumes — and stop. Staging, facts, scoring and features are all *interpretation*, and interpretation can happen at leisure. Only acquisition is on a clock.

**Sequencing consequence:** phase 3 comes before staging and quality gates, which is the reverse of the natural build order. That is intentional and is the single most important structural decision in this plan.

---

## Locked technical decisions

Settled here so they are not re-litigated mid-implementation.

| Decision | Choice | Reasoning |
|---|---|---|
| Python | **3.12** (matches the local 3.12.3) | Pinned in `.python-version`; Actions installs the same. |
| Environment | **`uv`** with committed `uv.lock` | Spec §9 requires local and Actions to share one resolved environment, since joblib artefacts are only loadable by a compatible stack. |
| DataFrames | **polars** | User's choice during design. |
| HTTP | **httpx** | Sync client, explicit timeouts, and `respx` gives clean offline tests. |
| CLI | **typer** | Spec §5. |
| Test / lint | **pytest**, **ruff** (lint + format) | Spec §11: "tooling kept proportionate". |
| Raw payload format | **gzipped JSON / NDJSON** + a `meta.json` sidecar | Spec §12 relies on gzipping for the storage budget. |
| Timestamps | UTC, ISO 8601 | One timezone in the data. UK local time appears only in human-facing output. |

### Path-safe `as_of` encoding

Partition directories are named `as_of=2026-08-01T03-30-00Z` — **colons replaced with hyphens**, because Windows forbids `:` in filenames and development is on Windows while execution is on Linux. Encoding and decoding live in exactly one place (`storage/paths.py`) and are covered by a round-trip test. The format remains lexicographically sortable, which the content-addressing lookup depends on.

---

## Phase 1 — Skeleton and storage

**Goal:** a working `fpl` command, and a storage layer that cannot corrupt data.
**Target:** 31 Jul – 3 Aug.

### 1.1 Project scaffold

- `uv init`, set `requires-python = ">=3.12,<3.13"`, create `.python-version`.
- `uv add polars httpx typer`; `uv add --dev pytest respx ruff`.
- Package at `src/fpl/`, `packages = ["src/fpl"]` in the build config.
- Configure ruff in `pyproject.toml`: line length 100, enable `E,F,I,UP,B,SIM`.
- Extend `.gitignore`: `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `scratch/`.

**Done when:** `uv run fpl --help` prints, `uv run pytest` collects zero tests and exits 0, `uv run ruff check .` is clean.

### 1.2 `config.py`

Frozen dataclass, no I/O, no network. Holds:

- `DATA_ROOT` (default `data/`, overridable by `FPL_DATA_ROOT` so tests never touch the real tree).
- `Season` — a small value type parsing `"2026-27"`, with `start_year`, and comparison so seasons sort.
- `CURRENT_SEASON`, `FIRST_ARCHIVE_SEASON = 2016-17`.
- `USER_AGENT` — descriptive, naming the project and a contact address (spec §13 defence 1).
- Per-source `min_request_interval` (FPL 2.0s, backfill 3.0s) and timeouts.

**Test:** season parsing, rejection of malformed strings, ordering, `FPL_DATA_ROOT` override.

### 1.3 `storage/paths.py`

The **only** module that knows the data layout. Everything else asks it for paths.

```python
raw_partition(source, endpoint, season, *, as_of=None, event=None, chunk=None) -> Path
staged_table(name, season) -> Path
facts_table(name, season, *, rules=None) -> Path
encode_as_of(dt) -> str        # 2026-08-01T03-30-00Z
decode_as_of(s) -> datetime
latest_partition(source, endpoint, season, *, event=None) -> Path | None
```

`latest_partition` lists sibling `as_of=*` directories and returns the lexicographic maximum — no mutable index file, so the raw tree stays purely append-only.

**Tests:** exact expected paths for each layer; `encode`/`decode` round-trip incl. sub-second truncation; `latest_partition` on empty, single and multi-partition trees; assert no produced path component contains `:`.

### 1.4 `storage/atomic.py`

`atomic_write_bytes(path, data)` — write to `path.with_suffix(".tmp")` in the *same directory* (so the rename stays on one filesystem), `fsync`, then `os.replace`. On any exception, unlink the temp file and re-raise.

**Tests:** normal write; simulated mid-write failure leaves neither a temp file nor a partial target; overwriting an existing file is atomic.

### 1.5 `storage/raw_io.py`

```python
@dataclass(frozen=True)
class RawArtifact:
    source: str; endpoint: str; season: Season
    url: str; params: dict; http_status: int
    body: bytes; fetched_at: datetime
    connector_version: str
    event: int | None = None

    @property
    def sha256(self) -> str: ...

def write_raw(artifact, *, force=False) -> WriteResult   # WriteResult(path, written: bool, reason: str)
def read_raw(path) -> tuple[bytes, dict]
```

`write_raw` is **content-addressed**: it reads the previous partition's `meta.json` and, if `sha256` matches, skips the write and returns `written=False`. Body is gzipped to `data.json.gz`; `meta.json` holds the metadata, written with `sort_keys=True` and a fixed separator so it is byte-stable.

> **Accepted trade-off.** Skipping means we do not record "at time T the source was unchanged". Freshness therefore comes from `status.json` and the workflow run history, not from the raw tree. This is the price of not rewriting an identical 250 KB blob nightly, and spec §12's storage projection assumes it.

**Tests:** first write creates both files; identical second write is skipped and leaves the first file's bytes and mtime untouched; changed body writes a new partition; `force=True` overrides; `read_raw` round-trips; `meta.json` is byte-identical across two runs with the same inputs.

### 1.6 `storage/parquet_io.py`

`write_parquet(df, path)` and `read_parquet(path)`, going through `atomic_write_bytes`. Determinism (spec §11) requires: fixed compression (`zstd`, fixed level), a canonical column order, and a caller-supplied sort key applied before writing.

**Test:** write the same frame twice → byte-identical files. Write with rows shuffled → still byte-identical.

### 1.7 `cli.py`

Typer app with `ingest`, `stage`, `facts`, `crosswalk`, `check`, `features`, `backfill`. Only `ingest` gains behaviour in phase 2; the rest exit 2 with "not implemented (phase N)" so the surface is visible and honestly unfinished.

Global `--verbose` and `--data-root`. Logging to stderr, structured `key=value`, so Actions logs stay greppable.

### 1.8 CI

`.github/workflows/ci.yml` — on push and PR: checkout, `astral-sh/setup-uv` with caching, `uv sync --locked` (fails if `uv.lock` is stale, satisfying spec §9), `uv run ruff check`, `uv run ruff format --check`, `uv run pytest`.

**Phase 1 exit criteria:** CI green on `master`; storage tests cover atomicity, content-addressing and determinism; no network code exists yet.

---

## Phase 2 — FPL API connector and raw ingestion

**Goal:** real FPL snapshots on disk, fetched by code that fails in the right way.
**Target:** 4 – 9 Aug.

### 2.1 `sources/errors.py`

The failure taxonomy from spec §10, as types — so call sites branch on class, not on status codes:

- `TransientError` — 5xx, timeouts, connection resets. Retryable.
- `RateLimitedError` — 429. Retryable after a longer backoff.
- `BlockedError` — **403. Never retryable.** Carries the response headers, since Cloudflare's `cf-ray` and `server` headers are what will distinguish a block from a genuine permission error when this fires at 03:30 one morning.
- `SchemaError` — the response parsed but did not look like what we expect.

### 2.2 `sources/base.py`

- `RawArtifact` (re-exported from storage).
- `RateLimiter` — enforces a minimum interval between requests on a per-host basis.
- `HttpFetcher.get_json(url, params)`:
  - sets `User-Agent` from config;
  - applies the rate limiter;
  - retries `TransientError` and `RateLimitedError` with exponential backoff **and jitter**, default 4 attempts;
  - raises `BlockedError` immediately on 403 with **no retry** (spec §10 — retrying into a block wastes requests and delays discovery);
  - **never upgrades `http://` to `https://`**, because Club Elo only answers on HTTP (spec §13). Asserted by test now, so a later "helpful" refactor cannot silently break phase 7.

**Tests (respx, no network):** 200 path; 500 → retries then succeeds; 500 always → `TransientError` after N attempts; 403 → `BlockedError` after **exactly one** request; 429 honours `Retry-After`; rate limiter enforces spacing under a fake clock; scheme is preserved.

### 2.3 `sources/fpl_api.py`

```python
class FplApiConnector:
    VERSION = "1"
    def bootstrap_static(self) -> RawArtifact
    def fixtures(self, *, event=None) -> RawArtifact
    def event_live(self, event: int) -> RawArtifact
    def element_summary(self, player_id: int) -> RawArtifact
```

No parsing beyond a shallow sanity check (`bootstrap-static` must contain non-empty `elements`, `teams`, `events`, else `SchemaError`). Interpretation belongs to staging.

### 2.4 Recorded fixtures

A dev-only script `scripts/record_fixtures.py` hits the real API once and writes trimmed payloads to `tests/fixtures/fpl/`. Trimming keeps ~5 players and ~2 teams but **every key**, so schema assertions stay meaningful while the repo does not carry a 500 KB blob per test. The trimming function is itself tested against a recorded full payload's key set.

### 2.5 `fpl ingest`

```
fpl ingest fpl --season 2026-27 --endpoint bootstrap-static
fpl ingest fpl --season 2026-27 --endpoint fixtures
fpl ingest fpl --season 2026-27 --endpoint event-live --event 1
fpl ingest fpl --season 2026-27 --endpoint element-summary --player 123
```

Idempotent, safe to re-run (content addressing makes a repeat a no-op). Logs one structured line per artifact: endpoint, status, bytes, sha prefix, written-or-skipped.

**Tests:** each endpoint writes the expected partition; a second identical run writes nothing; `BlockedError` exits non-zero with a distinct code so a workflow can branch on it.

### 2.6 First real run

Run `bootstrap-static` and `fixtures` against the live API and commit the result. This is the first real data in the repository, and the first honest measurement of the per-snapshot size assumed by spec §12 — record the actual figure and correct the spec if it is materially off.

**Phase 2 exit criteria:** real snapshots committed; CI green with no network access in tests; 403 handling proven by test.

---

## Phase 3 — Ownership capture, scheduled ⏰ **date-critical**

**Goal:** an unattended job that reliably captures the top 1,000 managers' picks from GW1.
**Target:** 10 – 16 Aug, with rehearsal 17 – 20 Aug.

### 3.1 Manager endpoints

Added to `FplApiConnector`:

```python
def classic_league_standings(self, league_id: int, page: int) -> RawArtifact
def entry_picks(self, entry_id: int, event: int) -> RawArtifact
```

> **Unverified until the season starts.** `entry/{id}/event/{gw}/picks/` could not be tested during design — 2026/27 had not begun and prior-season picks are not retained (spec §6.1). Live tools depend on it, so it is near-certainly public, but "near-certainly" is not "tested". **Re-probe in early August** and again the moment GW1's deadline passes. If it turns out to need authentication, that is a design-level problem and needs to surface with days of slack, not hours.

### 3.2 Chunked, resumable raw layout

Actions runners are ephemeral, so resume state must live in the repository. Chunking gives that while staying append-only:

```
data/raw/fpl/entry_picks/season=2026-27/event=1/
    chunk=0000/{data.ndjson.gz, meta.json}     entries   1– 100
    chunk=0001/{data.ndjson.gz, meta.json}     entries 101– 200
    ...
    chunk=0009/                                entries 901–1000
```

Resume is simply "which chunk directories already exist" — no lock file, no mutable state, no partial file ever rewritten. A run that dies at entry 640 leaves chunks 0000–0005 intact and the next run starts at 0006. Each chunk's `meta.json` carries its own `as_of`, since chunks may be captured minutes or hours apart.

`league_standings` is stored conventionally, one partition per capture, holding all 20 pages.

### 3.3 Target-event resolution

Pure function, no I/O — the part most likely to be wrong, so it is the part made easiest to test:

```python
def resolve_capture_event(bootstrap: dict, now: datetime, existing: set[int]) -> int | None
```

Returns the event where `deadline_time < now` **and** `not event.finished` **and** the event is not already fully captured. Otherwise `None`, and the job exits 0 having done nothing.

`finished` is the right bound rather than first kickoff: the rules state automatic substitutions are processed *at the end of the gameweek*, so the capture window is the whole gameweek — days, not the 90 minutes between deadline and kickoff (spec §6.1). The 30-minute schedule then has dozens of chances to complete a 35-minute job, instead of one chance to squeeze inside a narrow slot.

**Tests, table-driven:** before deadline → `None`; inside window → the event; `finished=true` → `None`; already captured → `None`; two events plausibly open (double gameweek boundaries) → the earlier; empty/absent events → `None`, never a crash.

### 3.4 Contamination check

Each pick payload carries `automatic_subs`, empty until the gameweek completes. **Non-empty means we captured too late and FPL has rewritten the XI.** The record is stored with `contaminated: true` in its chunk metadata rather than being mixed in as though it were a manager's decision.

This is cheap and it is the difference between "we believe the window logic is right" and "we can prove it was right for every row we kept". Silent contamination here would poison every EO feature downstream, and would be invisible.

**Test:** a payload with a non-empty `automatic_subs` is flagged; an empty one is not.

### 3.5 `fpl ingest fpl --endpoint ownership`

```
fpl ingest fpl --season 2026-27 --endpoint ownership --auto [--top 1000] [--limit N] [--dry-run]
```

1. Read the newest `bootstrap-static` (pull a fresh one if older than 6 h).
2. `resolve_capture_event`. If `None`, log `nothing_to_do` and exit 0.
3. Page `leagues-classic/314/standings` to collect the top *N* entry IDs. Store raw.
4. For each missing chunk, fetch 100 entries at the configured spacing, flag contamination, write the chunk.
5. Log progress per chunk so a killed run's position is legible in the Actions log.

`--limit` caps entries for rehearsal; `--dry-run` resolves the event and prints the plan without fetching. Both exist so the job can be exercised cheaply on the day rather than tested for the first time at full scale.

**Tests:** pagination across 20 mocked pages; resume skips existing chunks and fetches only the remainder; `--limit` respected; a mid-run `TransientError` leaves completed chunks valid and exits non-zero.

### 3.6 Workflows

Shared composite action `.github/actions/setup/` (checkout, uv, `uv sync --locked`) to avoid three copies of the same six lines.

Both workflows share `concurrency: { group: data-write, cancel-in-progress: false }` — queued, never cancelled, because cancelling a half-finished capture would be the one failure mode this design exists to prevent. Both need `permissions: contents: write`, commit as `github-actions[bot]`, and `git pull --rebase` before pushing.

**`daily-snapshot.yml`** — `30 3 * * *`. Ingests `bootstrap-static` and `fixtures`. Two requests. Runs after FPL's 01:30–02:30 UK price update (spec §3).

**`capture-ownership.yml`** — `*/30 * * * *`, plus `workflow_dispatch` with `limit` and `dry_run` inputs. Almost every run exits within seconds having found nothing to do; Actions minutes are unlimited on a public repo, so frequency is free. Runtime cap 90 min.

**Failure notification.** A reusable step opens or reuses a GitHub Issue on failure (spec §10). Implemented for `capture-ownership` in this phase — it is the job where silent failure is unrecoverable. The other workflows adopt it in phase 9.

### 3.7 Rehearsal — 17–20 Aug

Before the season starts, dry-run against a synthetic `bootstrap-static` with a manufactured open event, using a mock server, end to end through the real workflow via `workflow_dispatch`. This exercises scheduling, permissions, committing and pushing — the parts unit tests cannot reach — while there is still time to fix them.

**Phase 3 exit criteria:** `capture-ownership` scheduled and passing on `master`; a rehearsal run has committed and pushed; failure-to-issue verified by deliberately breaking a run once.

---

## Date-critical checklist

| When | Action | Why it cannot slip |
|---|---|---|
| Early Aug | Re-probe `entry/{id}/event/{gw}/picks/` | The one unverified assumption in the design (§3.1). Needs days of slack, not hours. |
| By 16 Aug | Phases 1–3 merged to `master`, workflows scheduled | Leaves a rehearsal buffer. |
| 17–20 Aug | Rehearsal via `workflow_dispatch` | Permissions, push and scheduling are untestable locally. |
| **21 Aug, deadline + 15 min** | `workflow_dispatch` with `limit: 20`, inspect output | A 2-minute end-to-end check on real data before trusting the full 35-minute run. |
| **21 Aug, deadline + 45 min** | Confirm the scheduled full run completed and committed 10 chunks | **GW1 picks are unrecoverable after the gameweek finishes.** |
| Each subsequent GW | Confirm chunks landed; investigate any `contaminated: true` | Same irreversibility, every week. |

---

## Later phases — sketch

Deliberately light. These will be planned properly once phases 1–3 are done and the shape of the real data is known; detail written now would be guesswork that goes stale.

| Phase | Scope | Key risk / note |
|---|---|---|
| **4. Staging and quality gates** | Typed polars schemas per source, `fpl stage`, `quality/` gates between layers. Drop `ep_next`, `form`, `xP` from archive imports. | Unknown-column warnings vs missing-column failures must be got right, or an FPL schema change passes silently. |
| **5. Scoring rules and facts** | `scoring/rules_2026_27.py` + golden cases from `docs/`, `facts/` assembly at `(season, fixture_id, player_id)`. | **Ends with points reconciliation passing against 2025/26 at zero tolerance** — the most important milestone in the subsystem. Nothing downstream is trustworthy until it does. |
| **6. Historical backfill and identity** | vaastav connector (single tarball, not per-file — spec §13), `identity/` crosswalk, `fpl backfill`. | Crosswalk review is manual and slow. Start it early; it parallelises with other work. |
| **7. Tier 2 sources** | Understat (all six leagues), Club Elo (**HTTP only**), football-data.co.uk, cup and European fixtures. | Cup/European fixture source is not yet pinned down (spec §16). |
| **8. Feature library** | `features/` with registry, `as_of` filtering, leakage test. | The leakage test is the phase's real deliverable; the features themselves belong to subsystem 3. |
| **9. Remaining automation** | `post-gameweek`, `pre-deadline`, `weekly-context`, failure-to-issue everywhere, `status.json`, live schema canary. | The schema canary is what surfaces upstream drift in August rather than November. |

Phases 4–5 are the correctness critical path. Phase 6 can start in parallel once phase 4 lands.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `entry/.../picks/` needs auth | Low | Probe in early August (§3.1). Fallback: capture a smaller sample via an authenticated session, or accept losing EO for 2026/27 and rely on global ownership. |
| Cloudflare starts blocking runners mid-season | Low — tested clear on 2026-07-30, but point-in-time | 403 is already a distinct non-retried failure that raises an issue (spec §13). Fallback is a self-hosted runner, which keeps the design intact. |
| Phases 1–3 overrun past 16 Aug | Medium | Phase 3 is the only date-critical part. If time compresses, cut phase 1 scope — parquet determinism and the full CLI surface can slip; atomic writes and content addressing cannot. |
| Capture job exceeds its window | Low | Window is the whole gameweek, not 90 min (§3.3). Chunked resume means partial progress always survives. |
| FPL changes response shapes at season rollover | Medium | Shallow sanity checks in phase 2 turn it into a loud failure. Phase 4's typed schemas make it precise. |

---

## Open questions

- Does the `entry_picks` endpoint impose its own rate limit stricter than the 2 s community norm? Unknown until GW1; the limiter is config-driven, so tightening it is a one-line change.
- Is top-1,000 the right sample, or should it widen to top-5,000 once the cost is measured for real? Deferred until after GW1, when the actual runtime is known rather than estimated.
