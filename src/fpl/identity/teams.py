"""The teams crosswalk: (season, team_id) -> team_code, canonical_name.

``team_id`` (FPL's numeric id) is reassigned every season and is **not**
reliably alphabetical (2025/26 lists Burnley at id=3 and Bournemouth at
id=4 - the reverse of alphabetical order - so that assumption was tried and
rejected during this build). ``team_code`` *is* genuinely stable across
seasons: Arsenal is code=3 in 2019/20 and in 2025/26 alike.

Seven seasons (2019/20 onward) publish teams.csv directly and are read from
it, building a ``team_code -> canonical_name`` lookup that is asserted
internally consistent (a code must never resolve to two different names
across those seven seasons).

The three earliest seasons (2016/17-2018/19, Finding 4) publish neither
teams.csv nor a team-name column anywhere in the archive. Their
``(team_id, team_code)`` pairs come from live data (``players_raw.csv``),
and are joined against the same lookup - every code that ever reappears in
a later season resolves for free. Six codes never reappear (their clubs
were relegated in this span and never returned by 2025/26): Stoke City,
Middlesbrough, Swansea City, Hull City, Huddersfield Town and Cardiff City.
Each was identified not by guesswork but by an unambiguous, verifiable
player-to-club fact for that exact season - e.g. code 110 carries Jack
Butland and Ryan Shawcross (Stoke's own goalkeeper and captain) in both
2016/17 and 2017/18; code 97 carries Neil Etheridge and Sol Bamba
(Cardiff's goalkeeper and captain) in 2018/19. See ``_HAND_VERIFIED_CODES``
for the full list and the reasoning per code.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.staging.base import decode_csv
from fpl.storage import paths
from fpl.storage.raw_io import read_raw

__all__ = ["build_teams_crosswalk", "write_teams_crosswalk"]

_TEAMS_CSV_SEASONS: tuple[str, ...] = (
    "2019-20",
    "2020-21",
    "2021-22",
    "2022-23",
    "2023-24",
    "2024-25",
    "2025-26",
)

_HAND_VERIFIED_CODES: dict[str, str] = {
    # Relegated during 2016/17-2018/19 and never back in the archive by
    # 2025/26, so their code never appears in any teams.csv we hold. Each
    # verified against an unambiguous real player for that club/season in
    # players_raw.csv itself (see module docstring).
    "110": "Stoke",  # Jack Butland, Ryan Shawcross - 2016/17 and 2017/18
    "25": "Middlesbrough",  # Victor Valdes, Daniel Ayala - 2016/17
    "80": "Swansea",  # Lukasz Fabianski, Angel Rangel - 2016/17 and 2017/18
    "88": "Hull",  # Allan McGregor, Oumar Niasse (on loan) - 2016/17
    "38": "Huddersfield",  # Jonas Lossl, Christopher Schindler - 2017/18 and 2018/19
    "97": "Cardiff",  # Neil Etheridge, Sol Bamba - 2018/19
}


def _season_from_teams_csv(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    partition = paths.latest_partition("vaastav", "teams", season, data_root=data_root)
    if partition is None:
        return None
    body, _meta = read_raw(partition)
    raw = decode_csv(body, "utf-8")
    return raw.select(
        pl.lit(str(season)).alias("season"),
        pl.col("id").cast(pl.Int64).alias("team_id"),
        pl.col("code").cast(pl.Utf8).alias("team_code"),
        pl.col("name").alias("canonical_name"),
    )


def _code_to_name_lookup(*, data_root: Path | None) -> dict[str, str]:
    """``team_code -> canonical_name``, built from every season that
    publishes ``teams.csv`` and asserted internally consistent."""
    lookup: dict[str, str] = dict(_HAND_VERIFIED_CODES)
    for season_str in _TEAMS_CSV_SEASONS:
        frame = _season_from_teams_csv(Season.parse(season_str), data_root=data_root)
        if frame is None:
            continue
        for code, name in frame.select("team_code", "canonical_name").iter_rows():
            existing = lookup.get(code)
            if existing is not None and existing != name:
                raise ValueError(
                    f"team_code {code!r} resolves to both {existing!r} and {name!r} "
                    f"across seasons; the 'team_code is stable' assumption has broken"
                )
            lookup[code] = name
    return lookup


def _season_from_players_raw(
    season: Season, *, data_root: Path | None, code_to_name: dict[str, str]
) -> pl.DataFrame | None:
    """The three earliest seasons: ``(team_id, team_code)`` from live data,
    with the name resolved via ``code_to_name`` - never by position."""
    partition = paths.latest_partition("vaastav", "players_raw", season, data_root=data_root)
    if partition is None:
        return None
    body, _meta = read_raw(partition)
    raw = decode_csv(body, "utf-8")
    codes = (
        raw.select(
            pl.col("team").cast(pl.Int64).alias("team_id"),
            pl.col("team_code").cast(pl.Utf8),
        )
        .unique()
        .sort("team_id")
    )
    unresolved = sorted(set(codes["team_code"].to_list()) - set(code_to_name))
    if unresolved:
        raise ValueError(
            f"{season}: team_code(s) {unresolved} have no known name; add them to "
            "_HAND_VERIFIED_CODES, verified against a real player for that club"
        )
    return codes.with_columns(
        pl.lit(str(season)).alias("season"),
        pl.col("team_code")
        .replace_strict(code_to_name, return_dtype=pl.Utf8)
        .alias("canonical_name"),
    ).select("season", "team_id", "team_code", "canonical_name")


def build_teams_crosswalk(seasons: list[Season], *, data_root: Path | None = None) -> pl.DataFrame:
    """Build (season, team_id, team_code, canonical_name) for every ingested
    season, preferring teams.csv where it exists."""
    code_to_name = _code_to_name_lookup(data_root=data_root)
    frames = []
    for season in sorted(seasons):
        frame = _season_from_teams_csv(season, data_root=data_root)
        if frame is None:
            frame = _season_from_players_raw(season, data_root=data_root, code_to_name=code_to_name)
        if frame is not None:
            frames.append(frame)
    if not frames:
        return pl.DataFrame(
            schema={
                "season": pl.Utf8,
                "team_id": pl.Int64,
                "team_code": pl.Utf8,
                "canonical_name": pl.Utf8,
            }
        )
    return pl.concat(frames).sort(["season", "team_id"])


def write_teams_crosswalk(crosswalk: pl.DataFrame, *, data_root: Path | None = None) -> Path:
    out_path = paths.crosswalk_file("teams.csv", data_root=data_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    crosswalk.write_csv(out_path)
    return out_path
