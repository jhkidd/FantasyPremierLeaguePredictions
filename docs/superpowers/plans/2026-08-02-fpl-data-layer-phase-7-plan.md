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

**Update — confirmed live 2026-08-03:** the user ran `GET /v4/competitions` with their own key and shared the England entries. The commonly-cited `ELC` is **not** the EFL Cup — it is the Championship (`id 2016, "Championship", plan TIER_ONE`). The two competitions this connector actually needs are:

| Competition | Code | id | `plan` tier |
|---|---|---|---|
| FA Cup | `FAC` | 2055 | `TIER_TWO` |
| Football League Cup (EFL Cup) | `FLC` | 2139 | `TIER_THREE` |

`numberOfAvailableSeasons` reports 145 (FAC) and 67 (FLC) in football-data.org's own system, but that is the competition's total historical depth, not necessarily what the free tier can serve per request — free tiers on this API commonly cap how far back fixture-list requests can go regardless of a competition's total recorded history. That depth limit is still to be confirmed empirically once the connector makes its first real backfill call (folded into task 15's runner-probe verification, not a separate blocker).

**Correction — confirmed live 2026-08-03, supersedes the coverage-page claim above:** the user attempted `GET /v4/competitions/FAC/matches` with their own (free-tier) key and it was rejected — the free tier does **not** include FA Cup or EFL Cup at all, despite the coverage page's earlier claim. The account's actual free-tier competition list is `PL` (Premier League), `ELC` (Championship), `CL` (Champions League), `WC` (World Cup), `EC` (European Championship) — no domestic cup of any kind. **This invalidates football-data.org as a source for FA Cup/EFL Cup fixtures under the free tier**, and with it the entire premise of splitting §18.1 the way Finding B proposed. Paused pending a decision with the user on how to proceed (drop domestic-cup congestion from scope, or find a different free source for just fixture dates/participants — no score or odds data is needed for this feature, only scheduling).

### Finding C — football-data.co.uk and openfootball formats, confirmed live

- `football-data.co.uk/mmz4281/2526/E0.csv` (E0 = Premier League): `Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,...` plus extensive opening/closing bookmaker odds columns (Bet365, Betfair, Pinnacle, market average). Team names are short forms ("Liverpool", "Newcastle", "Aston Villa") requiring the team crosswalk. One file per season; no domestic cup file exists on this site either (checked `englandm.php` directly — only league divisions are listed, confirming this site cannot be a fallback for Finding B).
- `openfootball/champions-league`: season directories confirmed for at least `2025-26/`; earliest season covered was not exhaustively checked and is a task-time detail rather than a design blocker, since the connector's own "season directory absent" handling (mirroring `VaastavConnector.extract_season`, which already treats a missing season directory as a hard failure requiring human classification) covers it correctly either way.

---

## Locked technical decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Cup/European fixture source | **Descoped domestic cups; Europe only** — `openfootball/champions-league` for Europe, plus FPL's own already-staged fixtures for Premier League congestion | Finding B found openfootball doesn't cover domestic cups; football-data.org (the proposed fallback) was then confirmed live (2026-08-03) to not serve FA Cup/EFL Cup on the free tier either. User decision: proceed without domestic-cup congestion for now, track finding a free source as future work (`future-domestic-cup-source`), rather than block this phase on it or pay for API access. |
| football-data.org | **Not built this phase** | Confirmed live (2026-08-03) that the free tier's competition list is `PL`, `ELC` (Championship), `CL`, `WC`, `EC` — no domestic cup of any kind, contradicting the earlier coverage-page-based Finding B claim. The config/secret scaffolding added in task 1 (`footballdataorg` `SourceConfig`, `FOOTBALL_DATA_API_KEY`) is left in place, unused, in case a future competition this account *can* access becomes useful — no connector is written against it this phase. |
| Club Elo scheme | **HTTP only, never HTTPS** | Already locked in spec §13; reconfirmed live (Finding A). |
| Club Elo same-day leakage | **Query the day *before* kickoff, never the fixture's own date** | Elo updates same-day after a match is played; querying the fixture's own date risks the rating already reflecting that day's result on early-kickoff days. Untested edge case, so the safer read is taken rather than assumed correct (consistent with "prefer feature-poor over leaky" from the phase 8 brainstorming). |
| Club Elo backfill granularity | **One request per distinct fixture date** (T-1), not per fixture | A gameweek has 3–4 distinct playing dates carrying ~10 fixtures each; Club Elo's per-date endpoint returns every club's rating in one call. Requesting per-fixture would be 10x more calls for identical data. |
| Team crosswalk | **New file** `crosswalk/team_external_ids.csv`, keyed on the existing `team_code` | The current `crosswalk/teams.csv` (phase 6) is auto-derived from FPL's own data and rebuilt by `crosswalk refresh` — not the right home for hand-reviewed external names. A second, small, hand-maintained file avoids repeating external names once per season row and keeps the auto-derived file's regeneration behaviour untouched. |
| Team crosswalk build method | **Auto-drafted via name-token matching, then human-reviewed** | ~25–30 clubs total; the same lightweight token-overlap check `identity/teams.py`/`identity/players.py` already use, not a new fuzzy-matching dependency. Draft is generated, reviewed once, then hand-maintained — never regenerated wholesale, so a reviewed correction is never silently overwritten. |
| Player crosswalk (FPL ↔ Understat) | **Fuzzy match, hand-reviewed CSV, hard fail on unmapped-with-minutes** | Reuses the mechanism spec §6/§14 already locked in; thousands of players, no shared stable key across sources, unlike the cross-season case (Finding 3, phases 4–6). |
| `facts/team_fixture` grain | **`(season, fixture_id, team_id)`** | Mirrors `facts/player_fixture`'s key discipline (spec §18.5). |
| Build order | Club Elo → `openfootball` → football-data.co.uk → ~~football-data.org~~ (descoped) → Understat | Cleanest/lowest-risk connectors first (spec §18.7); Understat (scraping, real fragility risk) last, and it also depends on the player crosswalk. football-data.org dropped from this phase's build order entirely (see above). |
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

- `api.clubelo.com/{YYYY-MM-DD}` — every club's rating as of that date. Backfill calls this **once per distinct fixture date across all ten seasons** (Locked decision above), using the day *before* kickoff to avoid same-day leakage. A helper collects the distinct `(date - 1 day)` set from FPL's own `fixtures` staged table before any Elo call is made, so the call count is bounded by real fixture dates, not iterated per fixture. Forward-looking predictions read the same endpoint at the current date, since a rating's `From`/`To` validity window already extends into the future until the next match is played.
- `api.clubelo.com/Fixtures` — **checked live during implementation and dropped, not built.** It exposes a full scoreline/goal-difference probability distribution (`Date,Country,Home,Away,GD<-5,...,R:0-0,R:0-1,...`), not the simple win/draw/loss breakdown the design assumed. Nothing in §18.5's locked columns (`elo_rating`, `opponent_elo_rating`) needs it, and the per-date endpoint above already covers forward-looking ratings — so building a parser for an unused, richer-than-needed shape would be pure YAGNI. Revisit only if a future column set wants match-outcome probabilities directly.

```python
class ClubEloConnector:
    VERSION = "1"
    SOURCE = "clubelo"

    def fetch_ratings(self, as_of_date: date) -> bytes: ...
    def artifact_for_ratings(self, body: bytes, as_of_date: date, season: Season) -> RawArtifact: ...
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

## 7.8–7.9 football-data.org (FA Cup / EFL Cup) — **descoped, not built this phase**

**Original goal:** domestic cup fixtures, the one credentialed connector in the project. **Confirmed live 2026-08-03: the free tier does not serve FA Cup or EFL Cup at all** (the account's actual free-tier competition list is `PL`, `ELC` (Championship), `CL`, `WC`, `EC` — no domestic cup of any kind), contradicting the coverage-page-based claim in Finding B. Tasks 3 and 7 are therefore dropped from this phase's build. The config/secret scaffolding from §7.1 (`footballdataorg` `SourceConfig`, `Config.football_data_api_key`) is left in place unused — harmless, and ready if a future need arises for one of the competitions this account *can* actually reach. Finding a free domestic-cup fixture source is tracked as future work (`future-domestic-cup-source`), not a blocker for the rest of this phase.

The sketch below was the original design, kept for reference only — it was never implemented:

```python
class FootballDataOrgConnector:
    VERSION = "1"
    SOURCE = "footballdataorg"

    FA_CUP_CODE = "FAC"        # confirmed against GET /v4/competitions (task 3), not assumed
    EFL_CUP_CODE = "FLC"       # confirmed the same way — do not hardcode from a web search

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


Rate limiting was intended to be enforced by `SourceConfig("footballdataorg", min_request_interval=6.0, ...)` (§7.1) rather than left to reactive 429 backoff — that `SourceConfig` entry remains in `config.py`, unused, since no connector is being built against it.

**This entire design is moot** (see the descoping note above) — the free tier cannot reach `FAC` or the EFL Cup code at all, so there is no historical depth to verify and no connector to test. Kept here only as a record of what was designed, in case a future free source (or a paid upgrade, if ever justified) revives this shape.

---

## 7.10–7.11 Understat — `sources/understat.py`, `staging/understat.py`

**Goal:** underlying-quality stats (xG, xA, shot quality) per player-fixture, via `understatapi` — built **last** in this phase (Locked decisions), after the credential-free and credentialed-but-simple connectors have proven the pattern still holds, and after the team crosswalk exists for it to join against.

This is the one connector in the phase with real fragility risk, since it scrapes rather than calling a documented API. **Update 2026-08-03:** built with our own `HttpFetcher` against Understat's embedded per-page JSON (script-tag payloads), not the third-party `understatapi` package — the user's call, to avoid a new dependency for what is, under the hood, one HTML fetch plus a small extraction step our existing tools already handle. `sources/understat.py` still wraps this behind the same `Connector` shape as everything else, so a future breakage is isolated to one module and one set of tests, never leaking into staging or facts.

The player crosswalk this connector needs (`crosswalk/players_fpl_understat.csv`) is genuinely different from the team crosswalk: thousands of players, no shared stable key, so it reuses the fuzzy-match-then-hand-review mechanism already locked in at spec §6/§14, with the same hard-fail-on-unmapped-with-minutes discipline as `identity/players.py::unmapped_players_with_minutes`.

**Tests:** recorded HTML/JSON fixtures from a real (trimmed) Understat response; a fragility smoke test that asserts the connector raises a clear `SchemaError` (not an obscure parse exception) if the page structure it depends on has changed, so a future breakage surfaces as "Understat's page changed" rather than a stack trace three layers down.

**Update 2026-08-04 (implementation — the 2026-08-03 note above is now itself stale):** a live probe against `understat.com` found the site has been redesigned since that note was written — team/league pages **no longer embed any JSON in a `<script>` tag at all**. The redesigned front end fetches its data through undocumented, same-origin AJAX endpoints instead (confirmed live via `curl`, cross-checked against the third-party `understatAPI` project's source as a map of what to probe, not as a dependency):

- Every request must carry the header `X-Requested-With: XMLHttpRequest` — omitting it returns a plain 404, indistinguishable from "this endpoint does not exist" until you know to check.
- `getLeagueData/{league}/{season}` returns one season's aggregate: a season-total row per player, plus a fixture list (ids, teams, final score/xG, but no player detail).
- `getMatchData/{match_id}` returns one fixture's per-player roster detail (minutes/goals/xG/xA/shots) — the genuine per-player-per-fixture grain the design needs.

This changes the request-volume shape materially: getting per-fixture player detail needs one `getMatchData` call **per match**, not per season (~380 EPL matches/season). At the existing `understat` `SourceConfig` politeness interval (2s) that is over two hours per season, ~20 hours for a full ten-season backfill — a different cost profile than every other Tier 2 source in this phase. Confirmed with the user (2026-08-04):

- **Scope: EPL only**, not the original design spec's (§18) six-league ambition — the per-match cost makes "the other five leagues cost little" no longer true.
- **Both endpoints are captured and staged**: `getLeagueData` yields `understat_players_season` (season aggregate, feeds the player crosswalk draft and cheap xG priors) and `understat_fixtures` (final score/xG only); `getMatchData` yields `understat_player_match` (the per-fixture grain).
- **Per-match capture is chunked and resumable**, mirroring `ownership.py`'s `write_chunk`/`iter_chunks` pattern rather than one raw partition per match (which would create ~3,800 tiny partitions over ten seasons) — implemented in `understat_capture.py`, batching `CHUNK_SIZE = 20` matches' `getMatchData` responses into one newline-delimited chunk.
- **Code + tests written and reviewed first**; the actual multi-hour historical backfill runs afterwards as its own step, not bundled into this implementation pass.

`sources/understat.py` still wraps this behind the same `Connector` shape as every other source (`VERSION`, `SOURCE`, `fetch_*`/`artifact_for_*`, `SchemaError` on an unexpected JSON shape), so this is a contained rewrite of one module's internals, not a design change visible to staging or facts.

**Update 2026-08-04 (tasks 10 and 11 complete):** both are now implemented, tested, and committed, and the full ten-season `getMatchData` backfill has run to completion.

- `identity/players_understat.py`'s matcher went through five bug-fix passes during human review of the drafted crosswalk, each triggered by a real mismatch the reviewer spotted, not a hypothetical: (1) the initial "shares any name token" check reused from `identity/players.py` was too loose and blocked correct matches (e.g. "James Ward-Prowse" vs "James Milner" both sharing "James"); replaced with a tiered `_best_understat_match` — exact normalised-name match, then a surname-only match requiring the candidate to be unique; (2) an ordered-subsequence pass was added for FPL's full legal names containing Understat's shorter public name in order (e.g. "Bernardo Mota Veiga de Carvalho e Silva" ⊃ "Bernardo Silva"); (3) `_normalize_name` was extended to HTML-unescape and strip apostrophes, fixing Irish names like "O'Connell" vs Understat's `O&#039;Connell` encoding; (4) a reordered-tokens pass was added for Japanese family-name-first vs given-name-first mismatches (e.g. "Sugawara Yukinari" vs "Yukinari Sugawara"); (5) a **correctness bug** was found in the surname-only pass from (1): requiring only a *unique* surname match among Understat candidates is not sufficient evidence of identity — it produced wrong-identity matches such as FPL's "Toby King" being matched to Understat's unrelated "Joshua King". Fixed by also requiring first-initial agreement, with a regression test added.
- After the fix in (5), diffing the drafted crosswalk against the reviewer's in-progress manual edits (to avoid clobbering them) surfaced 13 genuine same-surname-and-initial collisions between different real players (e.g. "Alex Robertson" vs "Andrew Robertson", "John McAtee" vs "James McAtee") that no name heuristic can safely resolve. Both sides of each collision (27 rows) were nulled out of `crosswalk/players_fpl_understat.csv` and moved to `crosswalk/players_fpl_understat_collisions.csv` for future human review — left unresolved for now, at the user's direction, since it's a small residual and late in the session.
- Final state: `crosswalk/players_fpl_understat.csv` has 1,845 of 2,643 player-codes matched; the remainder are either genuinely unmatched-with-minutes (64, ranked by total minutes played, tracked for future review) or the 27 collision rows above. `fpl crosswalk validate-understat` enforces the hard-fail-on-unmapped-with-minutes discipline going forward.
- The full 10-season `getMatchData` per-match backfill (task 11's outstanding runtime step) has completed and is committed — `understat_player_match` is staged for all ten seasons.
- Understat's staged data (season/player/match xG, xA, shots) is not yet joined into any `facts/` table — that join is follow-up work, not yet scheduled to a specific task.

---

## 7.12 Team crosswalk extension — `identity/team_external_ids.py`, `crosswalk/team_external_ids.csv`

**Goal:** one small, hand-maintained mapping from FPL's stable `team_code` (already established by `identity/teams.py` in phase 6) to each external source's own name for that club.

```
team_code, clubelo_name, understat_name, footballdata_couk_name
```

**Update 2026-08-03:** the `footballdataorg_id` column originally planned here is dropped — football-data.org is not being integrated this phase (see the descoping note in §7.8–7.9), so there is no source to key against. Re-added trivially (a new nullable column, additive only) if a domestic-cup source through that API ever materialises.

**Update 2026-08-03 (implementation):** a fifth column, `openfootball_name`, was added during implementation — this section's original table omitted it, but `facts/team_fixture`'s congestion count (§7.13) needs to resolve openfootball's European fixture team names back to a `team_code` too, and every other identity resolution in this project goes through a hand-reviewed crosswalk rather than a silent fuzzy match, so this source shouldn't be the one exception. Actual schema: `team_code, clubelo_name, understat_name, footballdata_couk_name, openfootball_name`.

Only ~25–30 distinct clubs span ten seasons across three sources — the same reasoning phase 6 used to reject fuzzy matching for the FPL-internal team crosswalk applies here too, but the mechanism differs because these are genuinely *external* names with no shared key at all (unlike `team_code`, which phase 6 already made a trivial join):

1. **Draft**: `identity/team_external_ids.py::draft_team_external_ids` generates a first-pass mapping using the same name-token-overlap check `identity/players.py::_shares_a_name_token` already implements (case/accent-insensitive shared-token match — "Man Utd"/"Manchester United" share no token by literal overlap alone, so the draft step also tries a small set of known FPL-style abbreviations already implicit in `identity/teams.py`'s `canonical_name`s, e.g. stripping "United"/"City" suffixes before comparing).
2. **Human review**: the draft is written to the same `crosswalk/team_external_ids.csv` path, then reviewed and corrected by hand — exactly the pattern already used for `crosswalk/players_fpl_understat.csv` (§7.10) and for the FPL-internal team crosswalk's `_HAND_VERIFIED_CODES` (phase 6). Unlike `crosswalk/teams.csv`, this file is **never regenerated wholesale** by `crosswalk refresh` once reviewed — refresh only *drafts new rows for codes not yet present*, never overwrites an existing row, so a hand correction is never silently clobbered by a re-run.
3. **Validate**: `fpl crosswalk validate` extended to check every `team_code` referenced by a staged Tier 2 table resolves to a row here — the same "unmapped-with-activity is a hard fail" discipline as the player crosswalk, adapted to teams: any club appearing in a staged Elo/openfootball/footballdata table with no crosswalk row fails the build.

**Tests:** the draft step correctly proposes matches for the ~10 known short-form/full-name pairs surfaced during probing (`Man City`↔`Manchester City`, `Spurs`↔`Tottenham`, `Newcastle`↔`Newcastle United`); a `crosswalk refresh` run twice never alters an already-reviewed row; an unmapped club with Tier 2 data present is a hard validation failure.

**Update 2026-08-03 (review complete):** `crosswalk/team_external_ids.csv` is hand-reviewed and committed; `fpl crosswalk validate` is clean. Two implementation-time findings surfaced while filling it in:

- **`ingest`/`stage` never actually dispatched on `clubelo`/`footballdata`/`openfootball`** — tasks 4–6 built the connectors and staging modules, but `cli.py` still only recognised `fpl`/`vaastav` and hit the phase-7 `_pending` stub for everything else, so no raw data existed for these sources to draft a crosswalk from at all. Closed by wiring all three into both commands (mirrors §7.14's already-planned surface — no new subcommand shape, just the missing dispatch branches).
- **A source column can hold more than one name for the same club.** `openfootball`'s own `football.txt` spells some clubs differently across seasons (`"Manchester City"` vs `"Manchester City FC"`). Rather than pick one and lose the other, each cell may now hold several `"; "`-joined aliases (`ALIAS_SEPARATOR` in `identity/team_external_ids.py`) — every alias a source has ever published for that club, all resolving to the same `team_code`.

Two real bugs were also found and fixed against live data during the historical backfill needed to populate this crosswalk (not specific to the crosswalk itself, but discovered here): football-data.co.uk serves its CSV with a leading UTF-8 BOM (broke the connector's header check — fixed via `utf-8-sig` decoding), and seasons before 2019-20 omit the `Time` column entirely (broke a strict header-prefix check — loosened to a subset-of-columns check). Club Elo's historical backfill (2016-17 through 2024-25) remains outstanding: the live service has been returning `502` for every date tried, confirmed as a genuine external outage rather than a sandbox/VPN issue — current-season Elo ratings (2025-26, 2026-27) are captured and sufficient for this crosswalk; the historical gap is tracked for a later retry.

## 7.13 `facts/team_fixture` — `src/fpl/facts/team_fixture.py`

**Goal:** the phase's one new facts table, at grain `(season, fixture_id, team_id)`, assembled by joining the staged Tier 2 tables through the team crosswalk and FPL's own `fixtures` staged table (mirroring `build_player_fixture_facts`'s shape, spec §18.5):

```python
@dataclass(frozen=True)
class TeamFixtureFactsResult:
    frame: pl.DataFrame
    rows: int
    unresolved_teams: tuple[str, ...]   # from any Tier 2 source, before crosswalk validation fails the build

def build_team_fixture_facts(season: Season, *, data_root: Path | None = None) -> pl.DataFrame | None: ...
def write_team_fixture_facts(season: Season, *, data_root: Path | None = None) -> TeamFixtureFactsResult: ...
```

Columns (spec §18.5, updated 2026-08-03 — see below):

- `elo_rating`, `opponent_elo_rating` — Club Elo, T-1 lookup (§7.2).
- `fixture_count_prior_N_days` — **renamed from `cup_fixture_count_prior_N_days`, domestic cups dropped from its inputs.** A trailing-window count combining FPL's own already-staged Premier League fixtures with `openfootball`'s European schedules, strictly before this fixture's kickoff. FA Cup/EFL Cup fixtures are not counted (no free source currently supplies them — tracked as `future-domestic-cup-source`); this undercounts true fixture congestion for clubs deep in a domestic cup run, which must not be forgotten when this column is consumed downstream. `N` is left as a small fixed set (7, 14, 28 days) rather than one value, since phase 8's later lasso-style predictor screening (a decision already recorded from the earlier brainstorming) is exactly the mechanism that will tell us which window matters — this phase's job is to make the windows available, not to pick one.
- `odds_implied_win_prob`, `odds_implied_draw_prob`, `odds_implied_loss_prob` — football-data.co.uk, overround-normalised (§7.6).

This table is silver: no rolling windows beyond the small fixed set above (which describe what happened, not an as-of feature), no point-in-time construction, no modelling. Phase 8 reads it exactly as it will read `facts/player_fixture`.

**Tests:** mirroring `tests/facts/test_player_fixture.py` — a golden case built from recorded fixtures across the Tier 2 staged tables plus a real `fixtures` snapshot, a key-uniqueness gate (`unique_key(["season", "fixture_id", "team_id"])`, reusing `quality/gates.py` directly), and a case where one Tier 2 source has no data for a given fixture (e.g. a club with no European involvement that season) — confirming the row still exists with nulls in only that source's columns, never a dropped row.

---

## 7.14 CLI additions

```
fpl stage clubelo|openfootball|footballdata|understat --season …
fpl facts team_fixture --season …
fpl check --layer facts --table team_fixture [--season …]
fpl crosswalk validate      # extended to cover team_external_ids and players_fpl_understat
fpl crosswalk refresh       # extended to draft new team_external_ids rows (never overwrite reviewed ones)
```

`fpl stage <source>` already dispatches on a source name (spec §8); this phase adds four new names to the same dispatch table (`footballdataorg` dropped — not built this phase), no new subcommand shape needed. `fpl facts` gains a second table argument (`player_fixture` was implicit/default before; `team_fixture` makes the choice explicit — a small, backwards-compatible CLI change).

## 7.15 Phase 7 exit criteria

- Four Tier 2 connectors (`clubelo`, `openfootball`, `footballdata`, `understat`) staged for at least the current season, with recorded-fixture tests passing offline. (`footballdataorg` descoped — see §7.8–7.9.)
- `crosswalk/team_external_ids.csv` and `crosswalk/players_fpl_understat.csv` committed, human-reviewed, and validating under `fpl crosswalk validate`.
- `facts/team_fixture` written for at least the current season, key-unique, with the leakage-relevant columns (`elo_rating`, `fixture_count_prior_N_days`) never referencing same-day or future information.
- Club Elo connectivity independently re-verified from an actual GitHub Actions runner (Finding A), not inferred from the sandbox result.

**Update 2026-08-04 (tasks 12 and 13 complete):** `facts/team_fixture` is implemented in `src/fpl/facts/team_fixture.py`, TDD throughout (`tests/facts/test_team_fixture.py`, 12 tests), full production rigor matching tasks 10/11. Key points versus the spec above:

- `fpl facts` was extended to always attempt both `player_fixture` and `team_fixture` in one run (rather than adding a second positional table argument) — simpler for the common case of running facts for a season, and each half independently reports `written`/`skipped` with its own detail message, so a season missing one Tier 2 source still gets the other table.
- `FACTS_TABLE_GATES["team_fixture"]` added to `src/fpl/quality/checks.py` with `unique_key(["season", "fixture_id", "team_id"])`; `fpl check --layer facts` now covers it automatically (no separate `--table` flag needed, since the existing check loop already iterates every registered facts table).
- Verified end-to-end against real staged 2026-27 data: 380 FPL fixtures → 760 `team_fixture` rows, `fpl check --layer facts` clean. `TeamFixtureFactsResult.unresolved_teams` surfaced ~470 club names from Club Elo/football-data.co.uk/openfootball that fall outside the Premier League crosswalk (foreign clubs in European competition legs, non-EPL leagues in the odds file) — this is expected and informational, not a blocking gate, since the crosswalk only needs to resolve the 20 current EPL teams.
- Two real implementation bugs were caught only by the real-data run (not the unit tests, whose fixtures didn't happen to exercise them): (1) the Elo T-1 join compared a `pl.Date` column against a raw `pl.Datetime` kickoff without normalising to calendar dates first, so a same-day rating (00:00) still counted as "before" a same-day kickoff (14:00) — fixed by taking `.date()` on the kickoff before filtering; (2) `sorted()` on the unresolved-team-name set raised `TypeError` because real data contains `None` team names in some source rows — fixed by filtering `None` out before sorting.
- Data committed: `data/staged/{teams,fixtures,events,players,price_snapshots,availability_snapshots}/season=2026-27/` (freshly staged from already-captured raw FPL data) and `data/facts/team_fixture/season=2026-27/part.parquet`.



## Cross-cutting

### Testing

Existing conventions hold unchanged: no network in CI, recorded/trimmed fixtures for every connector, `filterwarnings = ["error"]`, fixed date literals never season-relative, ruff line length 100. New fixture directories follow the existing `tests/fixtures/<source>/` convention; `scripts/record_fixtures.py` is extended with one capture function per new connector (mirroring how it already exists for the FPL API) rather than duplicated into new scripts.

**The leakage test stays in phase 8** — it tests `features.build`, which still does not exist. What phase 7 owns instead is making sure `facts/team_fixture` itself cannot leak (the T-1 Elo rule, the "strictly before kickoff" congestion window) — a narrower, source-level guarantee that phase 8 then builds on rather than has to re-derive.

### Risks

| # | Risk | Mitigation |
|---|---|---|
| **R1** | Club Elo connectivity from a real GitHub Actions runner is unverified — the sandbox's own 502 vs. `web_fetch`'s success (Finding A) proves nothing about the runner. | A throwaway workflow probe, identical in spirit to spec §13's original FPL-API probe, run before any Club Elo code is trusted in production. If it fails, Club Elo becomes a manual-backfill-only source rather than a scheduled one. |
| **R2** | *(Resolved 2026-08-03, no longer a risk.)* football-data.org's competition codes/season depth were assumed, not confirmed (Finding B). | Task 3 confirmed live that the free tier has no domestic-cup access at all — see the descoping note in §7.8–7.9. The connector is not being built this phase, so the original guess is moot rather than corrected. |
| **R3** | *(Moot — no connector built.)* football-data.org's 10 req/min limit is tighter than every other source in this project. | N/A. Left here as a record for if a future paid tier or free source revives this design. |
| R5 | Understat's page structure changes, breaking the scrape. | Isolated to `sources/understat.py`; a `SchemaError` names the failure clearly. Built last in this phase specifically so its instability does not block the other four sources. |
| R6 | `openfootball/champions-league`'s historical depth is shallower than ten seasons (unconfirmed — Finding C). | The season-directory-absent path (mirroring `vaastav`) fails loudly and requires a person to classify the gap, rather than silently producing zero-congestion rows that look like "no European fixtures" when the real answer is "no data". |
| R7 | The team crosswalk draft step mismatches a club (e.g. two clubs sharing a token, or a club renamed mid-decade). | Human review before commit, same as phase 6's `_HAND_VERIFIED_CODES` precedent; `fpl crosswalk validate`'s hard fail on any unmapped-but-active club catches anything the draft missed entirely. |
| R8 | Phase 7 collides with the 21 August GW1 rehearsal. | The rehearsal wins, exactly as phases 4–6's plan already established. Phase 7 has no deadline. |

### Deliberately out of scope

The features/gold library (phase 8, decisions parked in the session workspace). The chip-timing model. `fpl crosswalk refresh` regenerating a *reviewed* team or player crosswalk row (only ever adds new, never overwrites). Predicted lineups. **Domestic cup (FA Cup/EFL Cup) fixture congestion** — descoped 2026-08-03 once the free tier of football-data.org (the only source that was going to supply it) was confirmed live to have no domestic-cup access at all; tracked separately as `future-domestic-cup-source` for whenever a free alternative is found. Multi-GW horizon modelling. Any fitted model of any kind.

---

## Sequenced task list

| # | Task | Depends on |
|---|---|---|
| 1 | `config.py` — `openfootball`/`footballdataorg` `SourceConfig` entries, `football_data_api_key` field | — |
| 2 | `sources/fetcher.py` — optional per-request `headers` param | — |
| 3 | **Confirm football-data.org competition codes + season depth via `GET /v4/competitions`** — done; result was descoping, not a corrected build (see §7.8–7.9) | 1 |
| 4 | `sources/clubelo.py` + `staging/clubelo.py`, T-1 date logic | 1 |
| 5 | `sources/openfootball.py` + `staging/openfootball_parser.py` + `staging/openfootball.py` | 1 |
| 6 | `sources/footballdata.py` + `staging/footballdata.py` | 1 |
| ~~7~~ | ~~`sources/footballdataorg.py` + `staging/footballdataorg.py`~~ — **descoped, not built** | 1, 2, 3 |
| 8 | `identity/team_external_ids.py` + `crosswalk/team_external_ids.csv` (draft, then reviewed) | 4, 5, 6 |
| 9 | `crosswalk validate`/`refresh` extended for team_external_ids | 8 |
| 10 | `identity/players_understat.py` + `crosswalk/players_fpl_understat.csv` (draft, then reviewed) | — |
| 11 | `sources/understat.py` + `staging/understat.py` | 10 |
| 12 | `facts/team_fixture.py` | 8, 9, 11 |
| 13 | **Key-uniqueness + no-leakage checks on `facts/team_fixture`** | 12 |
| 14 | `fpl stage`/`fpl facts`/`fpl check` CLI wiring | 4–6, 12 |
| 15 | **Club Elo runner-connectivity probe (Finding A/R1)** | 4 |
| 16 | Record findings (confirmed competition codes, runner probe result, confirmed `openfootball` historical depth) back into spec §18 | 3, 15 |

Task 13 is the gate: it is the direct analogue of the phases 4–6 plan's reconciliation test — nothing built on top of `facts/team_fixture` is trustworthy until its key is provably unique and its point-in-time columns are provably non-leaky. Task 15 is the other hard gate for anything scheduled rather than manually run: phase 7 code must not go into a scheduled workflow until Club Elo is proven reachable from a real runner, not just from `web_fetch`.

