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
    """Fold accents, case, hyphens, and apostrophes away for a name-equality
    comparison (``Bešić`` == ``Besic``; ``Ward-Prowse`` tokenizes as ``ward
    prowse``; ``O'Connell`` == ``OConnell`` so it lines up with Understat's
    HTML-entity-encoded apostrophe, ``O&#039;Connell``, once that's also
    unescaped)."""
    import html
    import unicodedata

    unescaped = html.unescape(name)
    folded = unicodedata.normalize("NFKD", unescaped).encode("ascii", "ignore").decode().lower()
    return " ".join(folded.replace("-", " ").replace("'", "").split())


def _best_understat_match(fpl_name: str, candidates: pl.DataFrame) -> tuple[int, str] | None:
    """The Understat player who is ``fpl_name`` - ``None`` if that can't be
    said with confidence, left for a human to fill in rather than guessed at
    (mirrors ``team_external_ids._best_match``).

    Tried in two passes, each requiring a *unique* result before accepting
    it:

    1. Exact match on the full normalized name. This alone would still miss
       genuine spelling variants (``Muhamed Besic`` / ``Muhamed Bešić``,
       ``Matthew James`` / ``Matty James``), so:
    2. A surname-and-first-initial match: the same last name-token *and*
       the same first-name initial, when only one Understat candidate
       shares both. Surname alone is not enough - an earlier version of
       this pass matched on surname only and produced real wrong-identity
       assignments (FPL's ``Toby King`` incorrectly resolved to
       Understat's unrelated ``Joshua King``; ``Carl Stewart`` to
       ``Kevin Stewart``) purely because they happened to be the only
       Understat player with that surname that season, regardless of
       whether they were remotely the same person. Requiring the first
       initial too still accepts the spelling variants this pass exists
       for (``Muhamed Besic`` / ``Muhamed Bešić``, ``Matthew James`` /
       ``Matty James`` both start with ``M``), while no longer treating
       "shares a surname" as identity.
    3. A reordered-tokens match: the same set of name tokens, in any order,
       when only one candidate shares that exact set. Understat records
       some Japanese players surname-first as FPL does not - or vice versa
       (``Sugawara Yukinari`` / ``Yukinari Sugawara``) - so token order
       alone should not block a match once every token is accounted for on
       both sides.
    4. An ordered-subsequence match (:func:`_is_ordered_subsequence`): every
       token of the shorter name appears, in order, within the longer one.
       Catches FPL's occasional full legal name against Understat's shorter
       public one (``Bernardo Mota Veiga de Carvalho e Silva`` contains
       ``Bernardo`` then ``Silva``, in that order, matching Understat's
       ``Bernardo Silva``) that neither the exact nor surname-only pass
       would resolve.
    """
    normalized_target = _normalize_name(fpl_name)
    exact = {
        (row["understat_player_id"], row["understat_name"])
        for row in candidates.iter_rows(named=True)
        if _normalize_name(row["understat_name"]) == normalized_target
    }
    if len({player_id for player_id, _name in exact}) == 1:
        return next(iter(exact))

    target_tokens_for_surname = normalized_target.split()
    target_surname = target_tokens_for_surname[-1] if target_tokens_for_surname else ""
    target_first_initial = target_tokens_for_surname[0][0] if target_tokens_for_surname else ""
    surname_matches = {
        (row["understat_player_id"], row["understat_name"])
        for row in candidates.iter_rows(named=True)
        if target_surname
        if (candidate_tokens := _normalize_name(row["understat_name"]).split())
        if candidate_tokens[-1] == target_surname
        if candidate_tokens[0][:1] == target_first_initial
    }
    if len({player_id for player_id, _name in surname_matches}) == 1:
        return next(iter(surname_matches))

    target_tokens = normalized_target.split()
    reordered_matches = {
        (row["understat_player_id"], row["understat_name"])
        for row in candidates.iter_rows(named=True)
        if len(target_tokens) > 1
        if sorted(_normalize_name(row["understat_name"]).split()) == sorted(target_tokens)
    }
    if len({player_id for player_id, _name in reordered_matches}) == 1:
        return next(iter(reordered_matches))

    subsequence_matches = {
        (row["understat_player_id"], row["understat_name"])
        for row in candidates.iter_rows(named=True)
        if _is_ordered_subsequence(normalized_target, _normalize_name(row["understat_name"]))
    }
    if len({player_id for player_id, _name in subsequence_matches}) == 1:
        return next(iter(subsequence_matches))
    return None


def _is_ordered_subsequence(a: str, b: str) -> bool:
    """Do the whitespace tokens of the shorter of ``a``/``b`` all appear,
    in the same order, somewhere within the tokens of the longer one?

    Catches long-form legal names FPL sometimes records in full
    (``Bernardo Mota Veiga de Carvalho e Silva``) against Understat's
    shorter public name (``Bernardo Silva``) - ``bernardo`` and ``silva``
    both appear in the long name, in that order, with other tokens
    (``mota``, ``veiga``, ...) interspersed. Symmetric so it also catches
    the reverse shape, a short FPL name against a longer Understat one.
    """
    tokens_a = a.split()
    tokens_b = b.split()
    if not tokens_a or not tokens_b:
        return False
    if len(tokens_a) <= len(tokens_b):
        shorter, longer = tokens_a, tokens_b
    else:
        shorter, longer = tokens_b, tokens_a
    if len(shorter) < 2 or shorter == longer:
        # A single-token name is too weak a subsequence to trust, and an
        # identical pair would already have been caught by the exact pass.
        return False
    it = iter(longer)
    return all(token in it for token in shorter)


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
