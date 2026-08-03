"""Hand-reviewed crosswalk: FPL's stable ``team_code`` -> each Tier 2
source's own name string for that club (plan §7.12).

Only ~25-30 distinct clubs span ten seasons across these sources, so this is
a draft-then-review problem, not a join - unlike the FPL-internal team
crosswalk in ``identity/teams.py``, these are genuinely *external* names
with no shared stable key at all. The mechanism:

1. :func:`draft_team_external_ids` proposes a match for every FPL
   ``team_code`` using a loose name-token-overlap check (mirroring
   ``identity/players.py``'s ``_shares_a_name_token``), expanded with a
   small hardcoded table of known short forms ("Man Utd", "Spurs", "Wolves"
   ...) that share no literal token with the full name.
2. A human reviews and corrects the draft, committed to
   ``crosswalk/team_external_ids.csv``.
3. :func:`refresh_team_external_ids` never overwrites an already-reviewed
   row - it only adds a row for a ``team_code`` not yet present at all, so a
   ``crosswalk refresh`` re-run can never silently clobber a hand
   correction (unlike ``identity/teams.py``'s crosswalk, which is safely
   regenerated wholesale every time because it has no hand-review step to
   protect).
4. :func:`unmapped_source_names` gives ``crosswalk validate`` its hard-fail
   signal: a name a source actually published with no matching crosswalk
   row means real Tier 2 activity is invisible to the facts layer.

A source column may hold **several** :data:`ALIAS_SEPARATOR`-joined names
rather than one — confirmed necessary during the historical backfill
(2026-08-03), when ``openfootball``'s own ``football.txt`` files turned out
to spell the same club differently across seasons (``"Manchester City"`` in
some, ``"Manchester City FC"`` in others). One cell holds every distinct
alias a source has ever published for that club, so both resolve to the
same ``team_code`` without a second, un-reviewed fuzzy match at facts-
assembly time.

``openfootball_name`` was added to the schema during implementation
(2026-08-03) - the locked plan's §7.12 table only listed
``clubelo_name``/``understat_name``/``footballdata_couk_name``, but
``facts/team_fixture``'s congestion count (§7.13) also needs to resolve
openfootball's European fixture team names back to a ``team_code``, so the
same reviewed mechanism has to cover this source too. A purely mechanical
match with no persisted, human-reviewed record would otherwise be the one
unreviewed identity resolution in the whole project.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.sources.openfootball import SEASON_FILES as _OPENFOOTBALL_ENDPOINTS
from fpl.staging.clubelo import stage_ratings
from fpl.staging.footballdata import stage_matches_and_odds
from fpl.staging.openfootball import stage_fixtures as stage_openfootball_fixtures
from fpl.storage import paths
from fpl.storage.raw_io import partition_as_of, read_raw

__all__ = [
    "ALIAS_SEPARATOR",
    "TEAM_EXTERNAL_ID_COLUMNS",
    "collect_source_names",
    "draft_team_external_ids",
    "load_team_external_ids",
    "refresh_team_external_ids",
    "unmapped_source_names",
    "write_team_external_ids",
]

TEAM_EXTERNAL_ID_COLUMNS: tuple[str, ...] = (
    "team_code",
    "clubelo_name",
    "understat_name",
    "footballdata_couk_name",
    "openfootball_name",
)

_SOURCE_COLUMNS: tuple[str, ...] = TEAM_EXTERNAL_ID_COLUMNS[1:]

_SYNONYMS: dict[str, str] = {
    # Short/alternate forms seen across these sources that share no literal
    # name token with FPL's own canonical name - surfaced during phase 7
    # probing of Club Elo / football-data.co.uk / openfootball name columns.
    "man utd": "manchester united",
    "man united": "manchester united",
    "man city": "manchester city",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "nott'm forest": "nottingham forest",
    "nottm forest": "nottingham forest",
    "forest": "nottingham forest",
    "sheffield weds": "sheffield wednesday",
    "sheff wed": "sheffield wednesday",
    "west brom": "west bromwich albion",
    "brighton": "brighton and hove albion",
    "qpr": "queens park rangers",
    "leicester": "leicester city",
    "newcastle": "newcastle united",
    "west ham": "west ham united",
}


def _fold(name: str) -> str:
    """Case/accent-insensitive, punctuation-light normalisation."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    ascii_name = ascii_name.replace("&", "and").replace("-", " ").replace(".", "")
    for suffix in (" fc", " afc", " cf"):
        if ascii_name.endswith(suffix):
            ascii_name = ascii_name[: -len(suffix)]
            break
    return " ".join(ascii_name.split())


def _tokens(name: str) -> set[str]:
    folded = _fold(name)
    return set(_SYNONYMS.get(folded, folded).split())


def _shares_a_token(a: str, b: str) -> bool:
    return bool(_tokens(a) & _tokens(b))


def _best_match(source_name: str, fpl_names: dict[str, str]) -> str | None:
    """The one ``team_code`` whose canonical name shares a token with
    ``source_name`` - ``None`` if zero or more than one do, both left for a
    human to fill in rather than guessed at."""
    matches = [code for code, name in fpl_names.items() if _shares_a_token(source_name, name)]
    return matches[0] if len(matches) == 1 else None


ALIAS_SEPARATOR = "; "
"""Joins multiple alias strings a single source has published for the same
club across different seasons into one crosswalk cell (e.g. openfootball's
``football.txt`` format has switched between ``"Manchester City"`` and
``"Manchester City FC"`` over the years - both are the same club and both
must resolve, so a cell holds every distinct alias seen, not just the most
recent one)."""


def _split_aliases(cell: str | None) -> list[str]:
    if not cell:
        return []
    return [alias.strip() for alias in cell.split(ALIAS_SEPARATOR.strip()) if alias.strip()]


def draft_team_external_ids(
    fpl_teams: pl.DataFrame,
    *,
    clubelo_names: list[str] = (),
    understat_names: list[str] = (),
    footballdata_names: list[str] = (),
    openfootball_names: list[str] = (),
) -> pl.DataFrame:
    """One best-effort draft row per FPL ``team_code``, for a human to review.

    ``fpl_teams`` is ``identity/teams.py``'s ``(team_code, canonical_name)``
    crosswalk (any frame with those two columns; extra columns are ignored).
    Every code gets a row even when every source's match is ``None`` - a
    reviewer sees the whole club list and fills blanks in by hand, rather
    than a club being silently absent from the draft entirely.
    """
    fpl_names = dict(
        fpl_teams.select("team_code", "canonical_name").unique(subset=["team_code"]).iter_rows()
    )

    def _match_all(source_names: list[str]) -> dict[str, str]:
        resolved: dict[str, list[str]] = {}
        for source_name in source_names:
            code = _best_match(source_name, fpl_names)
            if code is not None and source_name not in resolved.get(code, []):
                resolved.setdefault(code, []).append(source_name)
        return {code: ALIAS_SEPARATOR.join(aliases) for code, aliases in resolved.items()}

    by_column = {
        "clubelo_name": _match_all(list(clubelo_names)),
        "understat_name": _match_all(list(understat_names)),
        "footballdata_couk_name": _match_all(list(footballdata_names)),
        "openfootball_name": _match_all(list(openfootball_names)),
    }

    rows = [
        {"team_code": code, **{col: mapping.get(code) for col, mapping in by_column.items()}}
        for code in sorted(fpl_names)
    ]
    return pl.DataFrame(rows, schema={col: pl.Utf8 for col in TEAM_EXTERNAL_ID_COLUMNS})


def load_team_external_ids(*, data_root: Path | None = None) -> pl.DataFrame:
    """The committed crosswalk, or an empty (correctly-typed) frame if
    nothing has been committed yet."""
    path = paths.crosswalk_file("team_external_ids.csv", data_root=data_root)
    if not path.exists():
        return pl.DataFrame(schema={col: pl.Utf8 for col in TEAM_EXTERNAL_ID_COLUMNS})
    return pl.read_csv(path, schema_overrides={col: pl.Utf8 for col in TEAM_EXTERNAL_ID_COLUMNS})


def refresh_team_external_ids(
    fpl_teams: pl.DataFrame,
    *,
    clubelo_names: list[str] = (),
    understat_names: list[str] = (),
    footballdata_names: list[str] = (),
    openfootball_names: list[str] = (),
    data_root: Path | None = None,
) -> pl.DataFrame:
    """Merge a fresh draft with whatever is already committed, *never*
    touching a row a human has already reviewed - only ever adding a row for
    a ``team_code`` not yet present at all (plan §7.12, point 2)."""
    existing = load_team_external_ids(data_root=data_root)
    draft = draft_team_external_ids(
        fpl_teams,
        clubelo_names=clubelo_names,
        understat_names=understat_names,
        footballdata_names=footballdata_names,
        openfootball_names=openfootball_names,
    )
    known_codes = set(existing["team_code"].to_list())
    new_rows = draft.filter(~pl.col("team_code").is_in(known_codes))
    if new_rows.height == 0:
        return existing
    if existing.height == 0:
        return new_rows
    return pl.concat([existing, new_rows])


def write_team_external_ids(crosswalk: pl.DataFrame, *, data_root: Path | None = None) -> Path:
    out_path = paths.crosswalk_file("team_external_ids.csv", data_root=data_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    crosswalk.write_csv(out_path)
    return out_path


def unmapped_source_names(
    source_names: list[str], crosswalk: pl.DataFrame, source_column: str
) -> list[str]:
    """Names a source actually published for which no crosswalk row's
    ``source_column`` matches - the hard-fail signal for ``crosswalk
    validate`` (plan §7.12, point 3): a club with real Tier 2 activity but
    no reviewed mapping must stop the build, not silently vanish.

    Each cell may hold several ``ALIAS_SEPARATOR``-joined names (a source
    that has published more than one spelling for the same club across
    seasons), so membership is checked against every alias, not the raw
    cell string."""
    if source_column not in _SOURCE_COLUMNS:
        raise ValueError(f"unknown source column: {source_column!r}")
    known: set[str] = set()
    if crosswalk.height:
        for cell in crosswalk[source_column].to_list():
            known.update(_split_aliases(cell))
    return sorted(set(source_names) - known)


def collect_source_names(
    seasons: list[Season], *, data_root: Path | None = None
) -> dict[str, list[str]]:
    """Distinct team-name strings each Tier 2 source has actually published,
    across every ingested season, read straight from raw captures (mirroring
    ``identity/teams.py``/``identity/players.py`` - crosswalks are built
    from raw, never from staged tables). A season with no raw capture on
    disk for a given source contributes nothing, exactly like those
    modules' existing "partition absent -> skip" behaviour. Understat (task
    11) is not yet built, so its list is always empty for now.
    """
    clubelo_names: set[str] = set()
    footballdata_names: set[str] = set()
    openfootball_names: set[str] = set()

    for season in sorted(seasons):
        elo_partition = paths.latest_partition("clubelo", "ratings", season, data_root=data_root)
        if elo_partition is not None:
            body, _meta = read_raw(elo_partition)
            as_of = partition_as_of(elo_partition).date()
            staged = stage_ratings(body, as_of, season)
            english = staged.frame.filter((pl.col("country") == "ENG") & (pl.col("level") == 1))
            clubelo_names |= set(english["club"].to_list())

        fd_partition = paths.latest_partition(
            "footballdata", "matches_and_odds", season, data_root=data_root
        )
        if fd_partition is not None:
            body, _meta = read_raw(fd_partition)
            staged = stage_matches_and_odds(body, season)
            footballdata_names |= set(staged.frame["home_team"].to_list())
            footballdata_names |= set(staged.frame["away_team"].to_list())

        for endpoint in _OPENFOOTBALL_ENDPOINTS.values():
            of_partition = paths.latest_partition(
                "openfootball", endpoint, season, data_root=data_root
            )
            if of_partition is None:
                continue
            body, _meta = read_raw(of_partition)
            staged = stage_openfootball_fixtures(body, season, endpoint)
            openfootball_names |= set(
                staged.frame.filter(pl.col("home_country") == "ENG")["home_team"].to_list()
            )
            openfootball_names |= set(
                staged.frame.filter(pl.col("away_country") == "ENG")["away_team"].to_list()
            )

    return {
        "clubelo_name": sorted(clubelo_names),
        "footballdata_couk_name": sorted(footballdata_names),
        "openfootball_name": sorted(openfootball_names),
        "understat_name": [],
    }
