"""Cross-season FPL player identity, keyed on the stable ``code`` field.

Finding 3: FPL's own numeric ``id`` is reassigned every season, but
``players_raw.csv``'s ``code`` field is stable. Of 270 players seen in both
2016/17 and 2019/20, 269 had a different ``id`` and every one of the 270 kept
the same ``code`` - nine of them even changed spelling (accents restored,
nicknames corrected) with zero collisions. Cross-season identity is therefore
a join, not a fuzzy-matching problem (spec Sec.6 amendment). Fuzzy matching
and a hand-reviewed CSV are reserved for cross-*source* identity (FPL <->
Understat <-> football-data), which is phase 7's problem, not this one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.facts.player_fixture import build_player_fixture_facts
from fpl.staging.base import decode_csv
from fpl.storage import paths
from fpl.storage.raw_io import read_raw

__all__ = [
    "PlayerCodeConflict",
    "build_players_crosswalk",
    "unmapped_players_with_minutes",
    "validate_name_variants",
    "write_players_crosswalk",
]

_PLAYERS_RAW_ENCODING = "utf-8"
"""``players_raw.csv`` is always UTF-8 regardless of a season's merged_gw
encoding, even in the two cp1252 eras - verified live against a known
accented name (Finding 3's debugging note)."""


def _season_players_raw(season: Season, *, data_root: Path | None = None) -> pl.DataFrame | None:
    """One season's ``(player_code, player_name, season)`` rows, or ``None``
    if that season's ``players_raw.csv`` has not been ingested."""
    partition = paths.latest_partition("vaastav", "players_raw", season, data_root=data_root)
    if partition is None:
        return None
    body, _meta = read_raw(partition)
    raw = decode_csv(body, _PLAYERS_RAW_ENCODING)
    return raw.select(
        pl.col("code").cast(pl.Utf8).alias("player_code"),
        (pl.col("first_name") + pl.lit(" ") + pl.col("second_name")).alias("player_name"),
    ).with_columns(pl.lit(str(season)).alias("season"))


def build_players_crosswalk(
    seasons: list[Season], *, data_root: Path | None = None
) -> pl.DataFrame:
    """Build ``(player_code, first_seen_season, last_seen_season,
    canonical_name, name_variants, seasons_seen)`` from every ingested
    season's ``players_raw.csv``.

    ``canonical_name`` is the spelling from the *most recent* season a code
    was seen in - later spellings restore accents earlier ones stripped
    (``Besic`` -> ``Bešić``), never the other way around.
    """
    frames = [
        frame
        for season in sorted(seasons)
        if (frame := _season_players_raw(season, data_root=data_root)) is not None
    ]
    if not frames:
        return pl.DataFrame(
            schema={
                "player_code": pl.Utf8,
                "first_seen_season": pl.Utf8,
                "last_seen_season": pl.Utf8,
                "canonical_name": pl.Utf8,
                "name_variants": pl.List(pl.Utf8),
                "seasons_seen": pl.Int64,
            }
        )

    combined = pl.concat(frames).sort("season")
    return (
        combined.group_by("player_code", maintain_order=True)
        .agg(
            pl.col("season").min().alias("first_seen_season"),
            pl.col("season").max().alias("last_seen_season"),
            pl.col("player_name").last().alias("canonical_name"),
            pl.col("player_name").unique(maintain_order=True).alias("name_variants"),
            pl.col("season").n_unique().alias("seasons_seen"),
        )
        .sort("player_code")
    )


def write_players_crosswalk(crosswalk: pl.DataFrame, *, data_root: Path | None = None) -> Path:
    out_path = paths.crosswalk_file("players_fpl.csv", data_root=data_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    crosswalk.with_columns(
        pl.col("name_variants").list.join("; ").alias("name_variants")
    ).write_csv(out_path)
    return out_path


@dataclass(frozen=True)
class PlayerCodeConflict:
    """A ``code`` whose name variants are not plausibly the same person.

    Surfaced for human review rather than auto-resolved (spec Sec.10) - a
    silently merged pair of different players is worse than a build that
    stops and asks."""

    player_code: str
    name_variants: tuple[str, ...]


def _shares_a_name_token(a: str, b: str) -> bool:
    """A loose plausibility check: do two spellings share at least one
    whitespace-separated token (case/accent-insensitive)?

    This accepts every variant Finding 3 actually found (``Muhamed Besic`` /
    ``Muhamed Bešić`` share ``Muhamed``; ``Matthew James`` / ``Matty James``
    share ``James``) while still catching a genuinely reused code, which is
    exceedingly unlikely to share any token at all."""
    import unicodedata

    def _tokens(name: str) -> set[str]:
        folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
        return set(folded.split())

    return bool(_tokens(a) & _tokens(b))


def validate_name_variants(crosswalk: pl.DataFrame) -> list[PlayerCodeConflict]:
    """Flag any ``code`` whose recorded name variants are not plausibly the
    same person - a real defect (a reused code) rather than a spelling fix."""
    conflicts: list[PlayerCodeConflict] = []
    for row in crosswalk.iter_rows(named=True):
        variants = tuple(row["name_variants"])
        if len(variants) <= 1:
            continue
        anchor = variants[-1]
        if any(not _shares_a_name_token(anchor, other) for other in variants[:-1]):
            conflicts.append(PlayerCodeConflict(row["player_code"], variants))
    return conflicts


def unmapped_players_with_minutes(
    season: Season, crosswalk: pl.DataFrame, *, data_root: Path | None = None
) -> list[int]:
    """Player ids that recorded minutes in this season's facts but whose
    ``player_code`` is absent from the crosswalk (or is null).

    Spec Sec.10: an unmapped player who actually played is a hard fail, since
    a silently dropped player is invisible while a failed build is not."""
    facts = build_player_fixture_facts(season, data_root=data_root)
    if facts is None or facts.height == 0:
        return []
    known_codes = set(crosswalk["player_code"].drop_nulls().to_list())
    played = facts.filter(pl.col("minutes") > 0)
    unmapped = played.filter(
        pl.col("player_code").is_null() | ~pl.col("player_code").is_in(list(known_codes))
    )
    return sorted(unmapped["player_id"].unique().to_list())
