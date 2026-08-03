"""Cross-source player identity: FPL's stable ``player_code`` <-> Understat's
own ``player_id`` (plan §7.10).

Genuinely different from ``identity/players.py``'s cross-*season* crosswalk:
there is no shared stable key between FPL and Understat at all, and
thousands of players span ten seasons, so this is draft-then-review, not a
join - the same shape ``identity/team_external_ids.py`` already established
for teams. Confirmed live (2026-08-04) that Understat's own ``player_id``
is itself stable across seasons for a real person (joining 2016/17 and
2025/26 season-aggregate rows by name found only two mismatches, both
genuine same-name-different-person collisions - Arsenal's Gabriel vs a
different Gabriel, two different Joshua Kings - not id churn), so mapping
FPL's ``player_code`` to Understat's ``player_id`` once is enough for every
season, exactly like the team crosswalk's one-row-per-club shape.

The mechanism mirrors ``team_external_ids.py``:

1. :func:`draft_players_crosswalk` proposes a match for every FPL
   ``player_code``, trying an exact normalized-name match first and
   falling back to a surname-only match (see :func:`_best_understat_match`
   for why a first-name-or-any-token overlap is too loose across sources).
   A draft match is only ever proposed when exactly one Understat player
   qualifies at whichever pass succeeds - the two genuine collisions found
   during probing (two different "Gabriel"s, two different "Joshua
   King"s) still have more than one same-surname candidate and are
   correctly left ``None`` for a human to resolve, rather than guessed at.
2. A human reviews and corrects the draft, committed to
   ``crosswalk/players_fpl_understat.csv``.
3. :func:`refresh_players_crosswalk` never overwrites an already-reviewed
   row - only ever adds a row for a ``player_code`` not yet present, same
   never-clobber discipline as the team crosswalk.
4. :func:`unmapped_understat_players_with_minutes` gives ``crosswalk
   validate`` its hard-fail signal: an Understat player who actually
   played real minutes with no matching crosswalk row means real
   underlying-quality data is invisible to facts assembly (spec Sec.10).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.staging.base import decode_csv
from fpl.storage import paths
from fpl.storage.raw_io import read_raw

__all__ = [
    "PLAYERS_UNDERSTAT_COLUMNS",
    "draft_players_crosswalk",
    "load_players_understat_crosswalk",
    "refresh_players_crosswalk",
    "understat_players_with_minutes",
    "unmapped_understat_players_with_minutes",
    "write_players_crosswalk",
]

PLAYERS_UNDERSTAT_COLUMNS: tuple[str, ...] = (
    "player_code",
    "fpl_name",
    "understat_player_id",
    "understat_name",
)


def _fpl_players(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    """One season's ``(player_code, fpl_name)`` from ``players_raw.csv`` -
    the same source ``identity/players.py`` reads."""
    partition = paths.latest_partition("vaastav", "players_raw", season, data_root=data_root)
    if partition is None:
        return None
    body, _meta = read_raw(partition)
    raw = decode_csv(body, "utf-8")
    return raw.select(
        pl.col("code").cast(pl.Utf8).alias("player_code"),
        (pl.col("first_name") + pl.lit(" ") + pl.col("second_name")).alias("fpl_name"),
    )


def _understat_players(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    """One season's ``(understat_player_id, understat_name)`` from the
    staged season-aggregate table (plan §7.11)."""
    partition = paths.latest_partition("understat", "league_data", season, data_root=data_root)
    if partition is None:
        return None
    from fpl.staging.understat import stage_league_players

    body, _meta = read_raw(partition)
    staged = stage_league_players(body, season)
    return staged.frame.select(
        pl.col("player_id").alias("understat_player_id"),
        pl.col("player_name").alias("understat_name"),
    )


def _normalize_name(name: str) -> str:
    """Fold accents, case, and hyphens away for a name-equality comparison
    (``Bešić`` == ``Besic``; ``Ward-Prowse`` tokenizes as ``ward prowse``)."""
    import unicodedata

    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return " ".join(folded.replace("-", " ").split())


def _best_understat_match(fpl_name: str, candidates: pl.DataFrame) -> tuple[int, str] | None:
    """The Understat player who is ``fpl_name`` - ``None`` if that can't be
    said with confidence, left for a human to fill in rather than guessed at
    (mirrors ``team_external_ids._best_match``).

    Tried in two passes, each requiring a *unique* result before accepting
    it:

    1. Exact match on the full normalized name. This alone would still miss
       genuine spelling variants (``Muhamed Besic`` / ``Muhamed Bešić``,
       ``Matthew James`` / ``Matty James``), so:
    2. A surname-only match: the same last name-token, when only one
       Understat candidate shares it. Plain first-name-or-any-token overlap
       (as ``identity/players.py``'s cross-season check uses, where a reused
       ``player_code`` would be the only cause of a false positive) is too
       loose across sources - shared first names are common enough
       (``James Ward-Prowse`` sharing ``James`` with ``James Milner``,
       ``James Tomkins``, ... ; ``Lucas Digne`` sharing ``Lucas`` with
       ``Lucas Moura``, ``Lucas Paquetá``, ...) that it produced false
       ambiguity, not just the genuine collisions (``Gabriel``, ``Joshua
       King``) found live during probing. Requiring the surname token to
       match keeps those genuine collisions correctly ambiguous while
       resolving names that only differ in first-name spelling or order.
    """
    normalized_target = _normalize_name(fpl_name)
    exact = {
        (row["understat_player_id"], row["understat_name"])
        for row in candidates.iter_rows(named=True)
        if _normalize_name(row["understat_name"]) == normalized_target
    }
    if len({player_id for player_id, _name in exact}) == 1:
        return next(iter(exact))

    target_surname = normalized_target.split()[-1] if normalized_target else ""
    surname_matches = {
        (row["understat_player_id"], row["understat_name"])
        for row in candidates.iter_rows(named=True)
        if _normalize_name(row["understat_name"]).split()[-1:] == [target_surname]
        if target_surname
    }
    if len({player_id for player_id, _name in surname_matches}) == 1:
        return next(iter(surname_matches))
    return None


def draft_players_crosswalk(
    seasons: list[Season], *, data_root: Path | None = None
) -> pl.DataFrame:
    """One best-effort draft row per FPL ``player_code`` seen across
    ``seasons``, for a human to review.

    Every FPL player_code with an ingested season is included even when no
    Understat match is found (both fields left ``None``) - a reviewer sees
    the whole player list and fills blanks in by hand, mirroring
    ``team_external_ids.draft_team_external_ids``'s "every code gets a row"
    behaviour. A season is tried in order and the first unambiguous match
    wins, so a player's most recent Understat appearance is not required to
    be the one that resolves them - any single season where the name is
    unambiguous is enough.
    """
    fpl_names: dict[str, str] = {}
    resolved: dict[str, tuple[int, str]] = {}

    for season in sorted(seasons):
        fpl_frame = _fpl_players(season, data_root=data_root)
        understat_frame = _understat_players(season, data_root=data_root)
        if fpl_frame is None:
            continue
        for code, name in fpl_frame.select("player_code", "fpl_name").iter_rows():
            fpl_names.setdefault(code, name)
            if understat_frame is not None and code not in resolved:
                match = _best_understat_match(name, understat_frame)
                if match is not None:
                    resolved[code] = match

    rows = [
        {
            "player_code": code,
            "fpl_name": name,
            "understat_player_id": resolved.get(code, (None, None))[0],
            "understat_name": resolved.get(code, (None, None))[1],
        }
        for code, name in sorted(fpl_names.items())
    ]
    return pl.DataFrame(
        rows,
        schema={
            "player_code": pl.Utf8,
            "fpl_name": pl.Utf8,
            "understat_player_id": pl.Int64,
            "understat_name": pl.Utf8,
        },
    )


def load_players_understat_crosswalk(*, data_root: Path | None = None) -> pl.DataFrame:
    """The committed crosswalk, or an empty (correctly-typed) frame if
    nothing has been committed yet."""
    path = paths.crosswalk_file("players_fpl_understat.csv", data_root=data_root)
    if not path.exists():
        return pl.DataFrame(
            schema={
                "player_code": pl.Utf8,
                "fpl_name": pl.Utf8,
                "understat_player_id": pl.Int64,
                "understat_name": pl.Utf8,
            }
        )
    return pl.read_csv(
        path,
        schema_overrides={
            "player_code": pl.Utf8,
            "fpl_name": pl.Utf8,
            "understat_player_id": pl.Int64,
            "understat_name": pl.Utf8,
        },
    )


def refresh_players_crosswalk(
    seasons: list[Season], *, data_root: Path | None = None
) -> pl.DataFrame:
    """Merge a fresh draft with whatever is already committed, *never*
    touching a row a human has already reviewed - only ever adding a row
    for a ``player_code`` not yet present at all (mirrors
    ``team_external_ids.refresh_team_external_ids``)."""
    existing = load_players_understat_crosswalk(data_root=data_root)
    draft = draft_players_crosswalk(seasons, data_root=data_root)
    known_codes = set(existing["player_code"].to_list())
    new_rows = draft.filter(~pl.col("player_code").is_in(known_codes))
    if new_rows.height == 0:
        return existing
    if existing.height == 0:
        return new_rows
    return pl.concat([existing, new_rows])


def write_players_crosswalk(crosswalk: pl.DataFrame, *, data_root: Path | None = None) -> Path:
    out_path = paths.crosswalk_file("players_fpl_understat.csv", data_root=data_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    crosswalk.write_csv(out_path)
    return out_path


def understat_players_with_minutes(
    season: Season, *, data_root: Path | None = None
) -> pl.DataFrame:
    """Understat players who recorded minutes this season, from the staged
    season-aggregate table - the population :func:`crosswalk_validate`
    checks against, mirroring ``identity/players.py``'s facts-derived
    check but sourced from Understat's own staged table since Understat
    (not FPL facts) is the side whose coverage is in question here."""
    from fpl.staging.understat import stage_league_players

    partition = paths.latest_partition("understat", "league_data", season, data_root=data_root)
    if partition is None:
        return pl.DataFrame(schema={"understat_player_id": pl.Int64, "understat_name": pl.Utf8})
    body, _meta = read_raw(partition)
    staged = stage_league_players(body, season)
    return (
        staged.frame.filter(pl.col("minutes") > 0)
        .select("player_id", "player_name")
        .rename({"player_id": "understat_player_id", "player_name": "understat_name"})
    )


def unmapped_understat_players_with_minutes(
    season: Season, crosswalk: pl.DataFrame, *, data_root: Path | None = None
) -> list[int]:
    """Understat player ids that recorded minutes this season but whose id
    is absent from the crosswalk (or the crosswalk row is otherwise
    unmapped) - spec Sec.10's hard-fail discipline, applied to Understat
    coverage rather than FPL's own player_code."""
    played = understat_players_with_minutes(season, data_root=data_root)
    if played.height == 0:
        return []
    known_ids = set(crosswalk["understat_player_id"].drop_nulls().to_list())
    unmapped = played.filter(~pl.col("understat_player_id").is_in(list(known_ids)))
    return sorted(unmapped["understat_player_id"].unique().to_list())
