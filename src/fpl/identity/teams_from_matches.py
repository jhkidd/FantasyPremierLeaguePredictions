"""Recover a season's ``teams`` table when the archive never published one.

Vaastav's ``teams.csv`` begins at 2019/20, and the FPL API serves only the
*current* season, so 2016/17-2018/19 have no ``team_id -> code`` mapping from
any first-party source. Without one ``facts/team_fixture`` cannot be built at
all for those seasons — no Elo, no odds, no congestion — which is a quarter of
the available training data.

The mapping is nonetheless fully determined by data already on disk. Both FPL
and football-data.co.uk describe the same 380 matches, so aligning the two on
``(kickoff date, home goals, away goals)`` pins each FPL ``team_id`` to a
football-data name, which the hand-reviewed ``crosswalk/team_external_ids.csv``
already maps to a stable ``team_code``.

Two properties make this safe rather than a guess:

* It is decided by **majority vote over a whole season**. Each club appears in
  38 matches, so a handful of ambiguous same-date-same-scoreline fixtures
  cannot move the result.
* It is **falsifiable**. :func:`derive_teams` refuses to return a partial
  mapping — anything other than a complete, one-to-one, 20-club assignment
  raises. It is also checked against real ``teams.csv`` ground truth for
  2019/20 onwards in the test suite, where it must reproduce all 20 rows
  exactly.

``name``/``short_name``/``strength`` cannot be recovered and are written null;
only ``team_id``, ``code`` and ``season`` are claimed. That is precisely what
``facts/team_fixture`` reads.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.identity.team_external_ids import load_team_external_ids
from fpl.staging.base import StagingReport
from fpl.staging.footballdata import parse_match_date
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet

__all__ = ["TEAMS_COLUMNS", "derive_teams", "derive_teams_from_frames"]

TEAMS_COLUMNS: tuple[str, ...] = (
    "season",
    "team_id",
    "code",
    "name",
    "short_name",
    "strength",
)
"""Column order of :data:`fpl.staging.fpl_api.TEAMS_SPEC`, matched exactly so a
derived season is indistinguishable downstream from a staged one."""

_EXPECTED_CLUBS = 20


def _alias_to_code(crosswalk: pl.DataFrame) -> dict[str, str]:
    """football-data's own name -> stable FPL ``team_code``, one entry per
    ``"; "``-joined alias."""
    mapping: dict[str, str] = {}
    for code, cell in crosswalk.select("team_code", "footballdata_couk_name").iter_rows():
        if not cell:
            continue
        for alias in cell.split(";"):
            alias = alias.strip()
            if alias:
                mapping[alias] = str(code)
    return mapping


def _vote(fixtures: pl.DataFrame, matches: pl.DataFrame) -> tuple[dict[int, Counter], int, int]:
    """Tally, for every FPL ``team_id``, which football-data club name it
    co-occurs with across the season.

    A fixture is aligned to a match on kickoff date *and* both scorelines. The
    scoreline is what disambiguates the several matches that share a date —
    and where it does not, the wrong candidates are noise spread thinly across
    many names while the right one accumulates ~38 votes.
    """
    votes: dict[int, Counter] = defaultdict(Counter)
    aligned = 0

    by_date: dict[object, list[dict]] = defaultdict(list)
    for match in matches.iter_rows(named=True):
        by_date[match["match_date"]].append(match)

    for fixture in fixtures.iter_rows(named=True):
        kickoff = fixture["kickoff_time"]
        if kickoff is None or fixture["team_h"] is None or fixture["team_a"] is None:
            continue
        kickoff_date = kickoff.date() if hasattr(kickoff, "date") else kickoff

        candidates = [
            match
            for match in by_date.get(kickoff_date, ())
            if match["full_time_home_goals"] == fixture["team_h_score"]
            and match["full_time_away_goals"] == fixture["team_a_score"]
        ]
        if not candidates:
            continue
        aligned += 1
        for match in candidates:
            votes[fixture["team_h"]][match["home_team"]] += 1
            votes[fixture["team_a"]][match["away_team"]] += 1

    return votes, aligned, fixtures.height


def _resolve(votes: dict[int, Counter], alias_to_code: dict[str, str], season: Season) -> dict:
    """Pick each team's winning name, then demand the result be a clean
    one-to-one assignment of 20 clubs before returning it."""
    chosen: dict[int, str] = {}
    for team_id, counter in votes.items():
        if not counter:
            continue
        (name, top), *rest = counter.most_common()
        if rest and rest[0][1] == top:
            raise ValueError(
                f"season {season}: team_id {team_id} ties between "
                f"{name!r} and {rest[0][0]!r} ({top} votes each); "
                "cannot resolve team identity unambiguously"
            )
        chosen[team_id] = name

    if len(chosen) != _EXPECTED_CLUBS:
        raise ValueError(
            f"season {season}: resolved {len(chosen)} of {_EXPECTED_CLUBS} clubs "
            f"({sorted(chosen)}); refusing to write a partial teams table"
        )
    if len(set(chosen.values())) != _EXPECTED_CLUBS:
        duplicated = [name for name, n in Counter(chosen.values()).items() if n > 1]
        raise ValueError(
            f"season {season}: {duplicated} resolved to more than one team_id; "
            "the fixture/match alignment is not one-to-one"
        )

    unmapped = sorted({name for name in chosen.values() if name not in alias_to_code})
    if unmapped:
        raise ValueError(
            f"season {season}: no crosswalk/team_external_ids.csv row for {unmapped}; "
            "add them before deriving this season's teams table"
        )
    return {team_id: alias_to_code[name] for team_id, name in chosen.items()}


def derive_teams_from_frames(
    fixtures: pl.DataFrame,
    matches: pl.DataFrame,
    crosswalk: pl.DataFrame,
    season: Season,
) -> tuple[pl.DataFrame, StagingReport]:
    """Derive ``(team_id, code)`` for one season from already-loaded frames."""
    alias_to_code = _alias_to_code(crosswalk)
    votes, aligned, total = _vote(fixtures, matches)
    resolved = _resolve(votes, alias_to_code, season)

    frame = pl.DataFrame(
        {
            "season": [str(season)] * len(resolved),
            "team_id": list(resolved),
            "code": [int(code) for code in resolved.values()],
        }
    ).with_columns(
        pl.lit(None, dtype=pl.Utf8).alias("name"),
        pl.lit(None, dtype=pl.Utf8).alias("short_name"),
        pl.lit(None, dtype=pl.Int64).alias("strength"),
    )
    frame = frame.select(list(TEAMS_COLUMNS)).sort("team_id")

    report = StagingReport(
        table="teams",
        rows_in=total,
        rows_out=frame.height,
        unknown_columns=(),
        excluded={"fixtures_unaligned": total - aligned},
    )
    return frame, report


def derive_teams(
    season: Season, *, data_root: Path | None = None
) -> tuple[pl.DataFrame, StagingReport] | None:
    """Derive one season's ``teams`` table from staged fixtures and matches.

    Returns ``None`` when either input is missing — there is nothing to derive
    from, a normal state rather than an error.
    """
    fixtures_path = paths.staged_table("fixtures", season, data_root=data_root) / "part.parquet"
    matches_path = (
        paths.staged_table("footballdata_matches_and_odds", season, data_root=data_root)
        / "part.parquet"
    )
    if not fixtures_path.exists() or not matches_path.exists():
        return None

    fixtures = read_parquet(fixtures_path).with_columns(
        pl.col("kickoff_time").str.strptime(
            pl.Datetime(time_unit="us", time_zone="UTC"), strict=False
        )
    )
    matches = read_parquet(matches_path).with_columns(
        parse_match_date(pl.col("match_date")).alias("match_date")
    )
    crosswalk = load_team_external_ids(data_root=data_root)
    return derive_teams_from_frames(fixtures, matches, crosswalk, season)
