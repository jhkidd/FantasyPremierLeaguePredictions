# Implementation Plan — Data Layer Phase 7

**Date:** 2026-08-02
**Spec:** [`../specs/2026-07-30-fpl-data-layer-design.md`](../specs/2026-07-30-fpl-data-layer-design.md) (§18)
**Predecessor:** [`2026-07-31-fpl-data-layer-phases-4-6-plan.md`](2026-07-31-fpl-data-layer-phases-4-6-plan.md)
**Covers:** phase 7 only. Phase 8 (features library) remains parked; phase 9 unchanged.

---

## Where we are going in

Phases 4–6 are complete: ten seasons of `facts/player_fixture` reconcile against FPL's published points at zero tolerance, under three rulesets, on a stable `code`-keyed player identity. That is the whole of what FPL's own data can tell us. Phase 7 adds everything FPL's API does not carry but the brainstorming in §18 decided the models need: opponent strength (Club Elo), fixture congestion from cup and European competitions, and market-implied match odds (football-data.co.uk) — plus, once identity resolution extends to a second axis (team) and a genuinely cross-source one (FPL ↔ Understat player identity), a first Understat connector for underlying-quality stats.

It ends with one new facts table, `facts/team_fixture`, at grain `(season, fixture_id, team_id)` — silver, not gold, exactly like `facts/player_fixture`. Nothing here builds a feature, a rolling window, or a model; that is phase 8, deliberately still parked (session file `phase-8-features-library-decisions.md`).

**Nothing here is date-critical**, for the same reason phases 4–6 weren't: this is historical and slowly-changing context data, not the live gameweek pipeline. The GW1 rehearsal checklist still takes precedence if it collides with this work.

---

## What probing the live sources changed

Before finalising the source list, each of the four candidate sources was probed directly — the same discipline that caught phases 4–6's Findings 1–8, and that caught a second, more consequential mistake here (see the second finding below).

### Finding A — Club Elo answers, but the sandbox's own network egress does not

Direct `curl.exe` calls to `http://api.clubelo.com` from this environment's shell returned `502 Bad Gateway`, reproducibly, with or without `--max-time`, on both `http://` and `https://`. The `web_fetch` tool, routed differently, retrieved the same URL successfully and returned real, current data:

```
Rank,Club,Country,Level,Elo,From,To
1,Arsenal,ENG,1,2063.7578125,2026-05-31,2026-08-21
```

This means the sandbox's own curl path cannot be used to validate runner connectivity — it is testing the sandbox's egress, not GitHub Actions'. Spec §13 already has a "tested and cleared" precedent for exactly this class of risk (Cloudflare classifying datacenter IPs as automated traffic). **The same probe must be re-run for Club Elo from an actual `ubuntu-latest` Actions runner before phase 7 code is trusted**, not inferred from the sandbox result either way. See Risk R1.

### Finding B — `openfootball` does not maintain domestic cup fixtures (already corrected in the spec)

The spec's first pass at §18.1 assumed `openfootball/england` covered FA Cup and EFL Cup, based on a web-search claim rather than direct verification. Checking the live repository (both `2025-26/` and `2018-19/` season directories via the GitHub contents API) found only league files — Premier League, Championship, League One, League Two, National League. Domestic cups exist in that repository only as one-off RSSSF archival snapshots (a single static 2019/20 FA Cup file, plus 1870s history) — not maintained per-season data. `openfootball/football.json` was checked too and has the same gap (league divisions and `uefa.cl.json` only, no domestic cup file, for 2024-25).

This was corrected before any implementation began: **§18.1 now splits the source in two** — `openfootball/champions-league` for European competitions (confirmed to work: season directories contain `cl.txt`, `clq.txt`, `confq.txt`, `elq.txt`), and **football-data.org**'s free tier for FA Cup / EFL Cup, accepted as the one documented exception to §13's no-credentials default. The user already holds a football-data.org account and has stored the key as the repository secret `FOOTBALL_DATA_API_KEY`. Confirmed directly against football-data.org's own coverage page: the free tier explicitly lists `England — FA Cup` and `England — Football League Cup`, at 10 requests/minute for registered clients.

**Still open, not yet verified live:** the exact competition codes (`FAC`/`ELC` are the commonly cited codes but web search results disagreed with each other on the EFL Cup code) and how many past seasons the free tier actually serves for these two competitions. Both must be confirmed against `GET /v4/competitions` using the stored key before the connector is written (task 3 below) — guessing a code risks silently querying the wrong competition rather than failing loudly.

### Finding C — football-data.co.uk and openfootball formats, confirmed live

- `football-data.co.uk/mmz4281/2526/E0.csv` (E0 = Premier League): `Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,...` plus extensive opening/closing bookmaker odds columns (Bet365, Betfair, Pinnacle, market average). Team names are short forms ("Liverpool", "Newcastle", "Aston Villa") requiring the team crosswalk. One file per season; no domestic cup file exists on this site either (checked `englandm.php` directly — only league divisions are listed, confirming this site cannot be a fallback for Finding B).
- `openfootball/champions-league`: season directories confirmed for at least `2025-26/`; earliest season covered was not exhaustively checked and is a task-time detail rather than a design blocker, since the connector's own "season directory absent" handling (mirroring `VaastavConnector.extract_season`, which already treats a missing season directory as a hard failure requiring human classification) covers it correctly either way.

---

## Locked technical decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Cup/European fixture source | **Split**: `openfootball/champions-league` for Europe, football-data.org (credentialed) for FA Cup/EFL Cup | Finding B. The only credential in this phase, and the only one in the whole project (§13 amendment). |
| Club Elo scheme | **HTTP only, never HTTPS** | Already locked in spec §13; reconfirmed live (Finding A). |
| Club Elo same-day leakage | **Query the day *before* kickoff, never the fixture's own date** | Elo updates same-day after a match is played; querying the fixture's own date risks the rating already reflecting that day's result on early-kickoff days. Untested edge case, so the safer read is taken rather than assumed correct (consistent with "prefer feature-poor over leaky" from the phase 8 brainstorming). |
| Club Elo backfill granularity | **One request per distinct fixture date** (T-1), not per fixture | A gameweek has 3–4 distinct playing dates carrying ~10 fixtures each; Club Elo's per-date endpoint returns every club's rating in one call. Requesting per-fixture would be 10x more calls for identical data. |
| Team crosswalk | **New file** `crosswalk/team_external_ids.csv`, keyed on the existing `team_code` | The current `crosswalk/teams.csv` (phase 6) is auto-derived from FPL's own data and rebuilt by `crosswalk refresh` — not the right home for hand-reviewed external names. A second, small, hand-maintained file avoids repeating external names once per season row and keeps the auto-derived file's regeneration behaviour untouched. |
| Team crosswalk build method | **Auto-drafted via name-token matching, then human-reviewed** | ~25–30 clubs total; the same lightweight token-overlap check `identity/teams.py`/`identity/players.py` already use, not a new fuzzy-matching dependency. Draft is generated, reviewed once, then hand-maintained — never regenerated wholesale, so a reviewed correction is never silently overwritten. |
| Player crosswalk (FPL ↔ Understat) | **Fuzzy match, hand-reviewed CSV, hard fail on unmapped-with-minutes** | Reuses the mechanism spec §6/§14 already locked in; thousands of players, no shared stable key across sources, unlike the cross-season case (Finding 3, phases 4–6). |
| `facts/team_fixture` grain | **`(season, fixture_id, team_id)`** | Mirrors `facts/player_fixture`'s key discipline (spec §18.5). |
| Build order | Club Elo → `openfootball` → football-data.co.uk → football-data.org → Understat | Cleanest/lowest-risk connectors first (spec §18.7); the one credentialed connector comes after the credential-free ones prove the pattern; Understat (scraping, real fragility risk) last, and it also depends on the player crosswalk. |
| Secret handling | `FOOTBALL_DATA_API_KEY` read from the environment only, never logged, never embedded in a raw artifact's URL or metadata | Standard secret hygiene; the connector fails loudly if the variable is unset rather than silently skipping the source (mirrors `identity/players.py`'s hard-fail-on-unmapped-minutes philosophy: an absent credential should be as loud as an absent player). |

---

## 7.1 Foundations: config, secrets, and the fetcher's header gap

**Goal:** every later section has a `SourceConfig` to read and a way to send a credential, without teaching the shared fetcher about credentials in general.

- `config.py`: add `SOURCES` entries for `openfootball` (tarball fetch, generous timeout like `vaastav`) and `footballdataorg` (simple GET, same politeness as `footballdata`, but a *stricter* `min_request_interval` derived from the documented 10 requests/minute — i.e. 6.0s, not the 2.0s used for the credential-free GET sources, so the connector itself enforces the limit rather than relying on retries after a 429).
- `Config`: add `football_data_api_key: str | None`, populated in `Config.load()` from `os.environ.get("FOOTBALL_DATA_API_KEY")`. Empty/whitespace-only is treated as unset (mirrors `_read_positive_int`'s blank-handling, but returns the trimmed string rather than parsing an int — a new small helper, not a reuse of `_read_positive_int` itself).
- `sources/fetcher.py`: `HttpFetcher.get` currently builds a fixed header dict (`User-Agent`, `Accept`) with no way for a caller to add to it. Add an optional `headers: Mapping[str, str] | None = None` parameter, merged **additively** over `_headers()` (never allowed to override `User-Agent`) so `sources/footballdataorg.py` can pass `{"X-Auth-Token": ...}` without the shared fetcher needing to know what a credential is. This is the only change to shared infrastructure in this phase.
- `sources/footballdataorg.py` fails fast and loudly at construction time if `Config.load().football_data_api_key` is `None` — never a silent 401 discovered three retries later.

**Tests:** `test_config.py` — key read from env, blank treated as unset, never appears in `repr(Config)` (a cheap guard against it ending up in a log line by accident). `test_fetcher.py` — extra headers reach the request, `User-Agent` is never overridden by a caller-supplied header of the same name.

---

## 7.2–7.3 Club Elo — `sources/clubelo.py`, `staging/clubelo.py`

**Goal:** pre-match Elo ratings and forward fixture probabilities, staged as typed tables, with the same "declared, not sniffed" discipline as every other source.

Two endpoints, both HTTP only (Finding A, §13):

- `api.clubelo.com/{YYYY-MM-DD}` — every club's rating as of that date. Backfill calls this **once per distinct fixture date across all ten seasons** (Locked decision above), using the day *before* kickoff to avoid same-day leakage. A helper collects the distinct `(date - 1 day)` set from FPL's own `fixtures` staged table before any Elo call is made, so the call count is bounded by real fixture dates, not iterated per fixture.
- `api.clubelo.com/Fixtures` — forward-looking win/draw/loss probabilities for scheduled matches, used for the current season's remaining fixtures (predictions, not backfill).

```python
class ClubEloConnector:
    VERSION = "1"
    SOURCE = "clubelo"

    def fetch_ratings(self, as_of_date: date) -> bytes: ...
    def fetch_upcoming_fixtures(self) -> bytes: ...
    def artifacts_for_date(self, body: bytes, as_of_date: date) -> list[RawArtifact]: ...
```

Staging (`staging/clubelo.py`): a `TableSpec` for `Rank,Club,Country,Level,Elo,From,To` (Finding: confirmed live CSV shape). `Club` is the source-name column the team crosswalk resolves against (§7.6). `Level` is retained — non-English clubs and lower-division English clubs both appear in a full daily pull, and filtering to Premier League opponents only happens at facts-assembly time (§7.7 downstream), not staging, so the staged table stays a faithful copy of what Club Elo actually published.

**Tests:** recorded fixture from the live CSV already captured during probing (trimmed to ~15 clubs, every column kept). A synthetic two-date case proves the T-1 lookup picks the date before kickoff, not the fixture date itself.

---

## 7.4–7.5 `openfootball` (European competitions) — `sources/openfootball.py`, `staging/openfootball.py`

**Goal:** Champions League / Europa League / Conference League fixture schedules, for congestion counting. Domestic cups are explicitly **not** this connector's job (Finding B).

Same tarball pattern as `sources/vaastav.py` — one archive fetch, never persisted whole, only the needed per-season competition files extracted as content-addressed raw artifacts:

```python
ARCHIVE_URL = "https://github.com/openfootball/champions-league/archive/refs/heads/master.tar.gz"

SEASON_FILES: dict[str, str] = {
    "cl.txt": "champions_league",
    "clq.txt": "champions_league_qualifying",
    "elq.txt": "europa_league_qualifying",
    "confq.txt": "conference_league_qualifying",
    # el.txt (Europa League group/knockout) referenced in the repo's README
    # but not independently confirmed in a directory listing during probing —
    # extracted if present, never required (see SEASON_FILES semantics below).
}
```

Unlike `vaastav.SEASON_FILES`, **none of these files are `required`** at the per-season level — a season with no European involvement for any tracked club is a legitimate, silent absence, not a defect. What *is* required, mirroring `VaastavConnector.extract_season`, is that the season directory itself exists in the archive; an unrecognised or missing season is still a hard failure needing human classification, exactly as it is for `vaastav`.

Staging (`staging/openfootball.py`): `football.txt` is a structured plain-text format, not CSV — the staging module's first job is parsing it into rows of `(date, home_team, away_team, competition, round)` before `stage_frame` can apply a `TableSpec` to it at all. This parsing step is the one piece of this connector genuinely novel relative to the CSV-shaped sources elsewhere in the pipeline, and gets its own small parser module (`staging/openfootball_parser.py`) with fixture-based tests independent of the network fetch.

**Tests:** a recorded `cl.txt`/`elq.txt` excerpt (a handful of matchdays, real formatting) drives parser tests; a season-directory-absent case asserts the same hard failure as `vaastav`; a season with zero European fixtures for any tracked club produces an empty (not missing) staged table.

---

## 7.6–7.7 football-data.co.uk (odds) — `sources/footballdata.py`, `staging/footballdata.py`

**Goal:** match-implied win/draw/loss probabilities, from bookmaker odds, normalised to remove the overround.

Simple GET, static CSV, no auth — `sources/footballdata.py` mirrors `sources/fpl_api.py`'s "simple GET" shape rather than `vaastav`'s tarball shape:

```python
def url_for_season(season: Season) -> str:
    # E0 = Premier League. mmz4281/{YY}{YY+1}/E0.csv
    ...

class FootballDataConnector:
    VERSION = "1"
    SOURCE = "footballdata"

    def fetch_season(self, season: Season) -> bytes: ...
    def artifacts_for_season(self, body: bytes, season: Season) -> list[RawArtifact]: ...
```

Staging declares only the columns the design actually uses — `Date, HomeTeam, AwayTeam, FTHG, FTAG, FTR` plus one representative closing-odds triple per outcome (`B365H, B365D, B365A` — Bet365 closing, the most consistently populated across all ten seasons per the probe) — leaving the dozens of other bookmaker/market columns as declared-unknown (a warning, not a failure, per the staging framework's existing asymmetry). Implied probabilities are computed at facts-assembly time (§7.12), normalised by dividing each raw implied probability by their sum, removing the overround — not stored raw, since a raw odds value is not itself the feature.

**Tests:** the confirmed live CSV shape (trimmed to ~10 rows, every declared column plus a sample of undeclared ones to prove the "unknown column → warning" path fires correctly). Team-name mismatches against the crosswalk (`Man Utd` vs `Manchester United`-style short forms) are exercised as a staging-to-facts join test, not a staging test — staging only types and selects, it does not resolve identity.

---

## 7.8–7.9 football-data.org (FA Cup / EFL Cup) — `sources/footballdataorg.py`, `staging/footballdataorg.py`

**Goal:** domestic cup fixtures, the one credentialed connector in the project.

```python
class FootballDataOrgConnector:
    VERSION = "1"
    SOURCE = "footballdataorg"

    FA_CUP_CODE = "FAC"        # confirmed against GET /v4/competitions (task 3), not assumed
    EFL_CUP_CODE = "..."       # confirmed the same way — do not hardcode from a web search

    def __init__(self, *, fetcher=None, config=None):
        api_key = (config or Config.load()).football_data_api_key
        if not api_key:
            raise SourceError(
                "FOOTBALL_DATA_API_KEY is not set; football-data.org requires a key "
                "(spec §13/§18.1 — the one credentialed source in this project)"
            )
        self._api_key = api_key
        ...

    def fetch_competition_matches(self, competition_code: str, season: Season) -> bytes:
        return self.fetcher.get(
            f"https://api.football-data.org/v4/competitions/{competition_code}/matches",
            params={"season": season.start_year},
            headers={"X-Auth-Token": self._api_key},
        ).body
```

Rate limiting is enforced by `SourceConfig("footballdataorg", min_request_interval=6.0, ...)` (§7.1) rather than left to reactive 429 backoff — deliberately conservative given this is the one source with a real (if generous) usage cap. A non-200, non-401 response is handled by the fetcher's existing classification (§10); a 401 specifically means the key is wrong or revoked, and is **never** retried (it falls into the generic 4xx branch already, which the fetcher does not retry — no fetcher change needed here beyond §7.1's header support).

**Historical depth is unverified** (Finding B) — task 3 in the sequenced list runs `GET /v4/competitions` with the real key and records what seasons are actually available for `FAC` and the EFL Cup code before any backfill code is written against an assumption.

Staging: match date, home/away team (names, resolved via the team crosswalk downstream — never in staging itself), and competition round. No odds or scoreline detail is needed here; this connector exists purely to produce fixture *dates* for the congestion count.

**Tests:** construction raises immediately when the key is absent (no network call attempted); a recorded response (once task 3 confirms the real shape) drives staging tests; the rate limiter's interval is asserted via `RateLimiter`'s existing injectable clock, same pattern as `tests/sources/test_fetcher.py`.

---

## 7.10–7.11 Understat — `sources/understat.py`, `staging/understat.py`

**Goal:** underlying-quality stats (xG, xA, shot quality) per player-fixture, via `understatapi` — built **last** in this phase (Locked decisions), after the credential-free and credentialed-but-simple connectors have proven the pattern still holds, and after the team crosswalk exists for it to join against.

This is the one connector in the phase with real fragility risk, since it scrapes rather than calling a documented API. `sources/understat.py` wraps `understatapi` behind the same `Connector` shape as everything else, so a future breakage is isolated to one module and one set of tests, never leaking into staging or facts.

The player crosswalk this connector needs (`crosswalk/players_fpl_understat.csv`) is genuinely different from the team crosswalk: thousands of players, no shared stable key, so it reuses the fuzzy-match-then-hand-review mechanism already locked in at spec §6/§14, with the same hard-fail-on-unmapped-with-minutes discipline as `identity/players.py::unmapped_players_with_minutes`.

**Tests:** recorded HTML/JSON fixtures from a real (trimmed) Understat response; a fragility smoke test that asserts the connector raises a clear `SchemaError` (not an obscure parse exception) if the page structure it depends on has changed, so a future breakage surfaces as "Understat's page changed" rather than a stack trace three layers down.

---

## 7.12 Team crosswalk extension — `identity/team_external_ids.py`, `crosswalk/team_external_ids.csv`

**Goal:** one small, hand-maintained mapping from FPL's stable `team_code` (already established by `identity/teams.py` in phase 6) to each external source's own name for that club.

```
team_code, clubelo_name, understat_name, footballdata_couk_name, footballdataorg_id
```

`footballdataorg_id` is numeric (the API's own team id), the others are the source's own name string. Only ~25–30 distinct clubs span ten seasons across four sources — the same reasoning phase 6 used to reject fuzzy matching for the FPL-internal team crosswalk applies here too, but the mechanism differs because these are genuinely *external* names with no shared key at all (unlike `team_code`, which phase 6 already made a trivial join):

1. **Draft**: `identity/team_external_ids.py::draft_team_external_ids` generates a first-pass mapping using the same name-token-overlap check `identity/players.py::_shares_a_name_token` already implements (case/accent-insensitive shared-token match — "Man Utd"/"Manchester United" share no token by literal overlap alone, so the draft step also tries a small set of known FPL-style abbreviations already implicit in `identity/teams.py`'s `canonical_name`s, e.g. stripping "United"/"City" suffixes before comparing).
2. **Human review**: the draft is written to the same `crosswalk/team_external_ids.csv` path, then reviewed and corrected by hand — exactly the pattern already used for `crosswalk/players_fpl_understat.csv` (§7.10) and for the FPL-internal team crosswalk's `_HAND_VERIFIED_CODES` (phase 6). Unlike `crosswalk/teams.csv`, this file is **never regenerated wholesale** by `crosswalk refresh` once reviewed — refresh only *drafts new rows for codes not yet present*, never overwrites an existing row, so a hand correction is never silently clobbered by a re-run.
3. **Validate**: `fpl crosswalk validate` extended to check every `team_code` referenced by a staged Tier 2 table resolves to a row here — the same "unmapped-with-activity is a hard fail" discipline as the player crosswalk, adapted to teams: any club appearing in a staged Elo/openfootball/footballdata/footballdataorg table with no crosswalk row fails the build.

**Tests:** the draft step correctly proposes matches for the ~10 known short-form/full-name pairs surfaced during probing (`Man City`↔`Manchester City`, `Spurs`↔`Tottenham`, `Newcastle`↔`Newcastle United`); a `crosswalk refresh` run twice never alters an already-reviewed row; an unmapped club with Tier 2 data present is a hard validation failure.

## 7.13 `facts/team_fixture` — `src/fpl/facts/team_fixture.py`

**Goal:** the phase's one new facts table, at grain `(season, fixture_id, team_id)`, assembled by joining the four staged Tier 2 tables through the team crosswalk and FPL's own `fixtures` staged table (mirroring `build_player_fixture_facts`'s shape, spec §18.5):

```python
@dataclass(frozen=True)
class TeamFixtureFactsResult:
    frame: pl.DataFrame
    rows: int
    unresolved_teams: tuple[str, ...]   # from any Tier 2 source, before crosswalk validation fails the build

def build_team_fixture_facts(season: Season, *, data_root: Path | None = None) -> pl.DataFrame | None: ...
def write_team_fixture_facts(season: Season, *, data_root: Path | None = None) -> TeamFixtureFactsResult: ...
```

Columns (spec §18.5, unchanged by this plan):

- `elo_rating`, `opponent_elo_rating` — Club Elo, T-1 lookup (§7.2).
- `cup_fixture_count_prior_N_days` — a trailing-window count combining `openfootball`'s European schedules and football-data.org's FA Cup/EFL Cup schedules, strictly before this fixture's kickoff. `N` is left as a small fixed set (7, 14, 28 days) rather than one value, since phase 8's later lasso-style predictor screening (a decision already recorded from the earlier brainstorming) is exactly the mechanism that will tell us which window matters — this phase's job is to make the windows available, not to pick one.
- `odds_implied_win_prob`, `odds_implied_draw_prob`, `odds_implied_loss_prob` — football-data.co.uk, overround-normalised (§7.6).

This table is silver: no rolling windows beyond the small fixed set above (which describe what happened, not an as-of feature), no point-in-time construction, no modelling. Phase 8 reads it exactly as it will read `facts/player_fixture`.

**Tests:** mirroring `tests/facts/test_player_fixture.py` — a golden case built from recorded fixtures across all four Tier 2 staged tables plus a real `fixtures` snapshot, a key-uniqueness gate (`unique_key(["season", "fixture_id", "team_id"])`, reusing `quality/gates.py` directly), and a case where one Tier 2 source has no data for a given fixture (e.g. a club with no European involvement that season) — confirming the row still exists with nulls in only that source's columns, never a dropped row.

---

## 7.14 CLI additions

```
fpl stage clubelo|openfootball|footballdata|footballdataorg|understat --season …
fpl facts team_fixture --season …
fpl check --layer facts --table team_fixture [--season …]
fpl crosswalk validate      # extended to cover team_external_ids and players_fpl_understat
fpl crosswalk refresh       # extended to draft new team_external_ids rows (never overwrite reviewed ones)
```

`fpl stage <source>` already dispatches on a source name (spec §8); this phase adds five new names to the same dispatch table, no new subcommand shape needed. `fpl facts` gains a second table argument (`player_fixture` was implicit/default before; `team_fixture` makes the choice explicit — a small, backwards-compatible CLI change).

## 7.15 Phase 7 exit criteria

- All five Tier 2 connectors (`clubelo`, `openfootball`, `footballdata`, `footballdataorg`, `understat`) staged for at least the current season, with recorded-fixture tests passing offline.
- `crosswalk/team_external_ids.csv` and `crosswalk/players_fpl_understat.csv` committed, human-reviewed, and validating under `fpl crosswalk validate`.
- `facts/team_fixture` written for at least the current season, key-unique, with the leakage-relevant columns (`elo_rating`, `cup_fixture_count_prior_N_days`) never referencing same-day or future information.
- Club Elo connectivity independently re-verified from an actual GitHub Actions runner (Finding A), not inferred from the sandbox result.
- football-data.org's actual competition codes and historical season depth for FA Cup/EFL Cup confirmed live (Finding B), and the connector built against the confirmed shape, not the initially-assumed one.
- `FOOTBALL_DATA_API_KEY` absence produces an immediate, clear construction-time failure — never a silent skip or an opaque 401 three retries in.

---

## Cross-cutting

### Testing

Existing conventions hold unchanged: no network in CI, recorded/trimmed fixtures for every connector, `filterwarnings = ["error"]`, fixed date literals never season-relative, ruff line length 100. New fixture directories follow the existing `tests/fixtures/<source>/` convention; `scripts/record_fixtures.py` is extended with one capture function per new connector (mirroring how it already exists for the FPL API) rather than duplicated into new scripts.

**The leakage test stays in phase 8** — it tests `features.build`, which still does not exist. What phase 7 owns instead is making sure `facts/team_fixture` itself cannot leak (the T-1 Elo rule, the "strictly before kickoff" congestion window) — a narrower, source-level guarantee that phase 8 then builds on rather than has to re-derive.

### Risks

| # | Risk | Mitigation |
|---|---|---|
| **R1** | Club Elo connectivity from a real GitHub Actions runner is unverified — the sandbox's own 502 vs. `web_fetch`'s success (Finding A) proves nothing about the runner. | A throwaway workflow probe, identical in spirit to spec §13's original FPL-API probe, run before any Club Elo code is trusted in production. If it fails, Club Elo becomes a manual-backfill-only source rather than a scheduled one. |
| **R2** | football-data.org's competition codes/season depth are still assumed, not confirmed (Finding B). | Task 3 confirms both against the live API before the connector is written against a guess. |
| **R3** | football-data.org's 10 req/min limit is tighter than every other source in this project. | A dedicated `SourceConfig` interval (6.0s) enforces it proactively rather than relying on 429 retries; usage is one or two requests per gameweek, comfortably inside the limit even so. |
| **R4** | `FOOTBALL_DATA_API_KEY` could be rotated, revoked, or expire without notice, silently breaking only the cup-congestion feature while everything else keeps working. | Construction-time check (fails if unset) plus a distinct, loud error path for a 401 at request time — never conflated with a transient failure that retries and eventually gives up quietly. |
| R5 | Understat's page structure changes, breaking the scrape. | Isolated to `sources/understat.py`; a `SchemaError` names the failure clearly. Built last in this phase specifically so its instability does not block the other four sources. |
| R6 | `openfootball/champions-league`'s historical depth is shallower than ten seasons (unconfirmed — Finding C). | The season-directory-absent path (mirroring `vaastav`) fails loudly and requires a person to classify the gap, rather than silently producing zero-congestion rows that look like "no European fixtures" when the real answer is "no data". |
| R7 | The team crosswalk draft step mismatches a club (e.g. two clubs sharing a token, or a club renamed mid-decade). | Human review before commit, same as phase 6's `_HAND_VERIFIED_CODES` precedent; `fpl crosswalk validate`'s hard fail on any unmapped-but-active club catches anything the draft missed entirely. |
| R8 | Phase 7 collides with the 21 August GW1 rehearsal. | The rehearsal wins, exactly as phases 4–6's plan already established. Phase 7 has no deadline. |

### Deliberately out of scope

The features/gold library (phase 8, decisions parked in the session workspace). The chip-timing model. `fpl crosswalk refresh` regenerating a *reviewed* team or player crosswalk row (only ever adds new, never overwrites). Predicted lineups. Any source requiring a credential beyond the one football-data.org exception. Multi-GW horizon modelling. Any fitted model of any kind.

---

## Sequenced task list

| # | Task | Depends on |
|---|---|---|
| 1 | `config.py` — `openfootball`/`footballdataorg` `SourceConfig` entries, `football_data_api_key` field | — |
| 2 | `sources/fetcher.py` — optional per-request `headers` param | — |
| 3 | **Confirm football-data.org competition codes + season depth via `GET /v4/competitions`** | 1 |
| 4 | `sources/clubelo.py` + `staging/clubelo.py`, T-1 date logic | 1 |
| 5 | `sources/openfootball.py` + `staging/openfootball_parser.py` + `staging/openfootball.py` | 1 |
| 6 | `sources/footballdata.py` + `staging/footballdata.py` | 1 |
| 7 | `sources/footballdataorg.py` + `staging/footballdataorg.py` | 1, 2, 3 |
| 8 | `identity/team_external_ids.py` + `crosswalk/team_external_ids.csv` (draft, then reviewed) | 4, 5, 6, 7 |
| 9 | `crosswalk validate`/`refresh` extended for team_external_ids | 8 |
| 10 | `identity/players_understat.py` + `crosswalk/players_fpl_understat.csv` (draft, then reviewed) | — |
| 11 | `sources/understat.py` + `staging/understat.py` | 10 |
| 12 | `facts/team_fixture.py` | 8, 9, 11 |
| 13 | **Key-uniqueness + no-leakage checks on `facts/team_fixture`** | 12 |
| 14 | `fpl stage`/`fpl facts`/`fpl check` CLI wiring | 4–7, 12 |
| 15 | **Club Elo runner-connectivity probe (Finding A/R1)** | 4 |
| 16 | Record findings (confirmed competition codes, runner probe result, confirmed `openfootball` historical depth) back into spec §18 | 3, 15 |

Task 13 is the gate: it is the direct analogue of the phases 4–6 plan's reconciliation test — nothing built on top of `facts/team_fixture` is trustworthy until its key is provably unique and its point-in-time columns are provably non-leaky. Task 15 is the other hard gate for anything scheduled rather than manually run: phase 7 code must not go into a scheduled workflow until Club Elo is proven reachable from a real runner, not just from `web_fetch`.

