"""Facts assembly: staged Tier 2 tables -> canonical ``team_fixture`` facts
(spec §18.5, plan §7.13).

Primary key is ``(season, fixture_id, team_id)`` — one row per team per
fixture, mirroring ``facts/player_fixture``'s key discipline (two rows per
fixture: the home team's and the away team's).

This table is **silver, not gold**: no rolling windows beyond the small
fixed 7/14/28-day fixture-congestion set below (which describe what
happened, not an engineered as-of feature), no point-in-time construction
beyond the Elo T-1 lookup and the strictly-before-kickoff congestion
window, no modelling. Phase 8 reads this table exactly as it reads
``facts/player_fixture``.

Every Tier 2 source's own team-name string is resolved to FPL's stable
``team_code`` (then to this season's ``team_id``) through the already
hand-reviewed ``crosswalk/team_external_ids.csv`` — this module does no
name matching of its own. A source name with no crosswalk row is collected
into :class:`TeamFixtureFactsResult.unresolved_teams` rather than silently
dropped, mirroring the player crosswalk's hard-fail-on-unmapped discipline.

A fixture with no recorded Tier 2 data for one source (e.g. a club with no
European involvement that season, or a date Club Elo never published a
rating for) still produces a row — nulls in only that source's columns,
never a dropped row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.identity.team_external_ids import load_team_external_ids
from fpl.staging.footballdata import parse_match_date
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet, write_parquet

__all__ = [
    "CONGESTION_WINDOWS",
    "KEY",
    "TeamFixtureFactsResult",
    "build_team_fixture_facts",
    "write_team_fixture_facts",
]

KEY: tuple[str, ...] = ("season", "fixture_id", "team_id")

CONGESTION_WINDOWS: tuple[int, ...] = (7, 14, 28)
"""Trailing-window sizes (days) for ``fixture_count_prior_N_days`` — a small
fixed set rather than one chosen value, since phase 8's later lasso-style
predictor screening is the mechanism that will tell us which window
matters (plan §7.13)."""


@dataclass(frozen=True)
class TeamFixtureFactsResult:
    frame: pl.DataFrame | None
    written: bool
    unresolved_teams: tuple[str, ...] = ()
    detail: str = ""


def _split_aliases(cell: str | None) -> list[str]:
    if not cell:
        return []
    return [alias.strip() for alias in cell.split(";") if alias.strip()]


def _name_to_code_map(crosswalk: pl.DataFrame, column: str) -> dict[str, str]:
    """One source column's name -> ``team_code``, expanding every
    ``"; "``-joined alias to its own dict entry."""
    mapping: dict[str, str] = {}
    if crosswalk.height == 0 or column not in crosswalk.columns:
        return mapping
    for code, cell in crosswalk.select("team_code", column).iter_rows():
        for alias in _split_aliases(cell):
            mapping[alias] = code
    return mapping


def _fpl_teams(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    teams_path = paths.staged_table("teams", season, data_root=data_root) / "part.parquet"
    if not teams_path.exists():
        return None
    return read_parquet(teams_path).select(["team_id", "code"])


def _fpl_fixtures(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    fixtures_path = paths.staged_table("fixtures", season, data_root=data_root) / "part.parquet"
    if not fixtures_path.exists():
        return None
    frame = read_parquet(fixtures_path)
    return frame.with_columns(
        pl.col("kickoff_time").str.strptime(
            pl.Datetime(time_unit="us", time_zone="UTC"), strict=False
        )
    )


def _clubelo_ratings(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    path = paths.staged_table("clubelo_ratings", season, data_root=data_root) / "part.parquet"
    if not path.exists():
        return None
    frame = read_parquet(path)
    return frame.with_columns(pl.col("as_of_date").str.strptime(pl.Date, strict=False))


def _footballdata_matches(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    path = (
        paths.staged_table("footballdata_matches_and_odds", season, data_root=data_root)
        / "part.parquet"
    )
    if not path.exists():
        return None
    frame = read_parquet(path)
    # Parsed leniently rather than with one fixed format: a partition staged
    # before the two-digit-year fix still holds ``DD/MM/YY``, which a plain
    # ``%d/%m/%Y`` reads as year 17 without complaint.
    return frame.with_columns(parse_match_date(pl.col("match_date")).alias("match_date"))


def _openfootball_fixtures(season: Season, *, data_root: Path | None) -> pl.DataFrame | None:
    directory = paths.staged_table("openfootball_fixtures", season, data_root=data_root)
    if not directory.is_dir():
        return None
    parts = sorted(directory.glob("competition=*.parquet"))
    if not parts:
        return None
    return pl.concat([read_parquet(part) for part in parts])


def _elo_for_team(
    elo: pl.DataFrame | None, code_lookup: dict[str, str], team_code: str, kickoff_date
) -> float | None:
    """The T-1 rating: the most recent Club Elo rating published **before**
    (never on or after) the fixture's kickoff date."""
    if elo is None:
        return None
    club_names = [name for name, code in code_lookup.items() if code == team_code]
    if not club_names:
        return None
    kickoff_calendar_date = kickoff_date.date() if hasattr(kickoff_date, "date") else kickoff_date
    candidates = elo.filter(
        pl.col("club").is_in(club_names) & (pl.col("as_of_date") < kickoff_calendar_date)
    )
    if candidates.height == 0:
        return None
    best = candidates.sort("as_of_date", descending=True).row(0, named=True)
    return best["elo"]


def _congestion_count(
    fpl_fixtures: pl.DataFrame,
    openfootball: pl.DataFrame | None,
    of_code_lookup: dict[str, str],
    team_code: str,
    team_id: int,
    kickoff: object,
    window_days: int,
) -> int:
    """Count of this team's own fixtures strictly before ``kickoff``, within
    the trailing ``window_days`` window — FPL's own Premier League fixtures
    plus openfootball's European ones, never same-day-or-later."""
    window_start = kickoff - _timedelta(days=window_days)

    fpl_count = fpl_fixtures.filter(
        ((pl.col("team_h") == team_id) | (pl.col("team_a") == team_id))
        & (pl.col("kickoff_time") < kickoff)
        & (pl.col("kickoff_time") >= window_start)
    ).height

    of_count = 0
    if openfootball is not None:
        club_names = [name for name, code in of_code_lookup.items() if code == team_code]
        if club_names:
            window_start_date = (kickoff - _timedelta(days=window_days)).date()
            kickoff_date = kickoff.date()
            of_count = openfootball.filter(
                ((pl.col("home_team").is_in(club_names)) | (pl.col("away_team").is_in(club_names)))
                & (pl.col("match_date") < kickoff_date)
                & (pl.col("match_date") >= window_start_date)
            ).height

    return fpl_count + of_count


def _timedelta(*, days: int):
    from datetime import timedelta

    return timedelta(days=days)


def build_team_fixture_facts(
    season: Season, *, data_root: Path | None = None
) -> pl.DataFrame | None:
    """Assemble one season's ``team_fixture`` facts from staged Tier 2 tables.

    Returns ``None`` when FPL's own ``fixtures``/``teams`` staged tables are
    not present — there is nothing to assemble against, a normal, expected
    state rather than an error, mirroring ``build_player_fixture_facts``.
    """
    result = _build_with_unresolved(season, data_root=data_root)
    if result is None:
        return None
    frame, _unresolved = result
    return frame


def _build_with_unresolved(
    season: Season, *, data_root: Path | None = None
) -> tuple[pl.DataFrame, list[str]] | None:
    fpl_fixtures = _fpl_fixtures(season, data_root=data_root)
    fpl_teams = _fpl_teams(season, data_root=data_root)
    if fpl_fixtures is None or fpl_teams is None:
        return None

    crosswalk = load_team_external_ids(data_root=data_root)
    clubelo_lookup = _name_to_code_map(crosswalk, "clubelo_name")
    footballdata_lookup = _name_to_code_map(crosswalk, "footballdata_couk_name")
    openfootball_lookup = _name_to_code_map(crosswalk, "openfootball_name")

    elo = _clubelo_ratings(season, data_root=data_root)
    footballdata = _footballdata_matches(season, data_root=data_root)
    openfootball = _openfootball_fixtures(season, data_root=data_root)

    team_id_to_code = dict(fpl_teams.select("team_id", "code").iter_rows())
    team_id_to_code = {tid: str(code) for tid, code in team_id_to_code.items()}

    rows: list[dict] = []
    for fixture in fpl_fixtures.iter_rows(named=True):
        kickoff = fixture["kickoff_time"]
        for team_id, opponent_id in (
            (fixture["team_h"], fixture["team_a"]),
            (fixture["team_a"], fixture["team_h"]),
        ):
            team_code = team_id_to_code.get(team_id)
            opponent_code = team_id_to_code.get(opponent_id)

            elo_rating = (
                _elo_for_team(elo, clubelo_lookup, team_code, kickoff)
                if team_code and kickoff is not None
                else None
            )
            opponent_elo_rating = (
                _elo_for_team(elo, clubelo_lookup, opponent_code, kickoff)
                if opponent_code and kickoff is not None
                else None
            )

            congestion = {
                f"fixture_count_prior_{window}_days": (
                    _congestion_count(
                        fpl_fixtures,
                        openfootball,
                        openfootball_lookup,
                        team_code,
                        team_id,
                        kickoff,
                        window,
                    )
                    if kickoff is not None
                    else None
                )
                for window in CONGESTION_WINDOWS
            }

            win_prob = draw_prob = loss_prob = None
            if footballdata is not None and team_code is not None:
                home_names = [n for n, c in footballdata_lookup.items() if c == team_code]
                away_names = [n for n, c in footballdata_lookup.items() if c == opponent_code]
                match_date = kickoff.date() if kickoff is not None else None
                candidates = footballdata.filter(
                    (
                        (pl.col("home_team").is_in(home_names))
                        & (pl.col("away_team").is_in(away_names))
                        & (pl.col("match_date") == match_date)
                    )
                    | (
                        (pl.col("home_team").is_in(away_names))
                        & (pl.col("away_team").is_in(home_names))
                        & (pl.col("match_date") == match_date)
                    )
                )
                if candidates.height:
                    match = candidates.row(0, named=True)
                    home_odds = match["bet365_home_odds"]
                    draw_odds = match["bet365_draw_odds"]
                    away_odds = match["bet365_away_odds"]
                    if home_odds and draw_odds and away_odds:
                        raw_home = 1 / home_odds
                        raw_draw = 1 / draw_odds
                        raw_away = 1 / away_odds
                        overround = raw_home + raw_draw + raw_away
                        is_home = match["home_team"] in home_names
                        if is_home:
                            win_prob, draw_prob, loss_prob = (
                                raw_home / overround,
                                raw_draw / overround,
                                raw_away / overround,
                            )
                        else:
                            win_prob, draw_prob, loss_prob = (
                                raw_away / overround,
                                raw_draw / overround,
                                raw_home / overround,
                            )

            rows.append(
                {
                    "season": str(season),
                    "fixture_id": fixture["fixture_id"],
                    "team_id": team_id,
                    "opponent_team_id": opponent_id,
                    "was_home": team_id == fixture["team_h"],
                    "elo_rating": elo_rating,
                    "opponent_elo_rating": opponent_elo_rating,
                    **congestion,
                    "odds_implied_win_prob": win_prob,
                    "odds_implied_draw_prob": draw_prob,
                    "odds_implied_loss_prob": loss_prob,
                }
            )

    unresolved: set[str] = set()
    if elo is not None:
        unresolved |= set(elo["club"].to_list()) - set(clubelo_lookup)
    if footballdata is not None:
        unresolved |= set(footballdata["home_team"].to_list()) - set(footballdata_lookup)
        unresolved |= set(footballdata["away_team"].to_list()) - set(footballdata_lookup)
    if openfootball is not None:
        unresolved |= set(openfootball["home_team"].to_list()) - set(openfootball_lookup)
        unresolved |= set(openfootball["away_team"].to_list()) - set(openfootball_lookup)

    frame = pl.DataFrame(
        rows,
        schema={
            "season": pl.Utf8,
            "fixture_id": pl.Int64,
            "team_id": pl.Int64,
            "opponent_team_id": pl.Int64,
            "was_home": pl.Boolean,
            "elo_rating": pl.Float64,
            "opponent_elo_rating": pl.Float64,
            **{f"fixture_count_prior_{w}_days": pl.Int64 for w in CONGESTION_WINDOWS},
            "odds_implied_win_prob": pl.Float64,
            "odds_implied_draw_prob": pl.Float64,
            "odds_implied_loss_prob": pl.Float64,
        },
    )

    dupes = frame.select(list(KEY)).is_duplicated().sum()
    if dupes:
        raise ValueError(
            f"team_fixture key {KEY} is not unique in season {season}: {dupes} duplicate row(s)"
        )

    frame = frame.with_columns()
    return frame, sorted(name for name in unresolved if name is not None)


def write_team_fixture_facts(
    season: Season, *, data_root: Path | None = None
) -> TeamFixtureFactsResult:
    """Build and write ``facts/team_fixture/season=.../part.parquet``.

    Idempotent and deterministic — an unchanged rebuild produces an empty
    Git diff (spec §11)."""
    result = _build_with_unresolved(season, data_root=data_root)
    if result is None:
        return TeamFixtureFactsResult(
            None, False, detail="no fixtures/teams staged for this season"
        )
    frame, unresolved = result

    out_dir = paths.facts_table("team_fixture", season, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet", sort_by=list(KEY))
    return TeamFixtureFactsResult(frame, True, unresolved_teams=tuple(unresolved))
