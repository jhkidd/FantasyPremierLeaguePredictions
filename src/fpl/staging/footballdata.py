"""Stage football-data.co.uk's per-season match-and-odds CSV.

Only the columns the design actually uses are declared: the result itself
(``Date, HomeTeam, AwayTeam, FTHG, FTAG, FTR``) plus one representative
closing-odds triple (``B365H, B365D, B365A`` — Bet365, the most consistently
populated bookmaker across all ten seasons per the probe). Every other
bookmaker/market column football-data.co.uk publishes is left as
declared-unknown (a warning, not a failure, per the staging framework's
existing asymmetry) — implied probabilities are computed at facts-assembly
time from the raw odds (plan §7.12), normalised to remove the overround,
never stored raw as a feature.

Team names are short forms ("Liverpool", "Newcastle") requiring the team
crosswalk — resolved at facts-assembly time, not here. Staging only types
and selects.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from fpl.config import Season
from fpl.staging.base import ColumnSpec, StagingReport, TableSpec, decode_csv, stage_frame

__all__ = ["MATCHES_AND_ODDS_SPEC", "StagedMatchesAndOdds", "stage_matches_and_odds"]

MATCHES_AND_ODDS_SPEC = TableSpec(
    table="footballdata_matches_and_odds",
    key=("match_date", "home_team", "away_team"),
    encoding="utf-8-sig",
    columns=(
        ColumnSpec("match_date", "Date", pl.Utf8),
        ColumnSpec("home_team", "HomeTeam", pl.Utf8),
        ColumnSpec("away_team", "AwayTeam", pl.Utf8),
        ColumnSpec("full_time_home_goals", "FTHG", pl.Int64),
        ColumnSpec("full_time_away_goals", "FTAG", pl.Int64),
        ColumnSpec("full_time_result", "FTR", pl.Utf8),
        ColumnSpec("bet365_home_odds", "B365H", pl.Float64, required=False),
        ColumnSpec("bet365_draw_odds", "B365D", pl.Float64, required=False),
        ColumnSpec("bet365_away_odds", "B365A", pl.Float64, required=False),
    ),
)


@dataclass(frozen=True)
class StagedMatchesAndOdds:
    frame: pl.DataFrame
    report: StagingReport


def stage_matches_and_odds(body: bytes, season: Season) -> StagedMatchesAndOdds:
    """Stage one season's match-and-odds CSV.

    ``match_date`` is kept as the raw ``DD/MM/YYYY`` string football-data.co.uk
    publishes rather than parsed into a date here — facts assembly already
    parses every other source's date strings at the point it joins them
    against FPL's own fixture calendar (spec §18), so a second, independent
    date-parsing path here would only be a second place for a format
    assumption to drift out of step with that one.
    """
    raw = decode_csv(body, MATCHES_AND_ODDS_SPEC.encoding)
    staged, report = stage_frame(raw, MATCHES_AND_ODDS_SPEC)
    staged = staged.with_columns(pl.lit(str(season)).alias("season")).select(
        ["season", *staged.columns]
    )
    return StagedMatchesAndOdds(frame=staged, report=report)
