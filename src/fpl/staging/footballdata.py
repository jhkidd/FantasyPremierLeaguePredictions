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

__all__ = [
    "MATCHES_AND_ODDS_SPEC",
    "StagedMatchesAndOdds",
    "parse_match_date",
    "stage_matches_and_odds",
]

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


def _to_iso_date(column: str) -> pl.Expr:
    """Normalise football-data.co.uk's ``DD/MM/YY`` *or* ``DD/MM/YYYY`` to ISO.

    The publisher switched from two- to four-digit years between 2016/17 and
    2017/18. This matters far more than it looks: ``%d/%m/%Y`` does not
    *reject* ``"01/01/17"``, it silently reads it as **17 AD**, so a consumer
    sees a fully populated, entirely wrong date column and every join against
    it quietly matches nothing.
    """
    return parse_match_date(pl.col(column)).dt.strftime("%Y-%m-%d").alias(column)


def parse_match_date(column: pl.Expr) -> pl.Expr:
    """Parse a football-data ``match_date`` in any format it has been stored in.

    Accepts the ISO form written since the two-digit-year fix as well as both
    published ``DD/MM/YY`` and ``DD/MM/YYYY`` forms, so a consumer reading a
    partition staged before that fix still gets correct dates instead of
    silently wrong ones.
    """
    return (
        pl.when(column.str.contains(r"^\d{4}-\d{2}-\d{2}$"))
        .then(column.str.strptime(pl.Date, format="%Y-%m-%d", strict=False))
        .when(column.str.contains(r"^\d{1,2}/\d{1,2}/\d{2}$"))
        .then(column.str.strptime(pl.Date, format="%d/%m/%y", strict=False))
        .otherwise(column.str.strptime(pl.Date, format="%d/%m/%Y", strict=False))
    )


def stage_matches_and_odds(body: bytes, season: Season) -> StagedMatchesAndOdds:
    """Stage one season's match-and-odds CSV.

    ``match_date`` is normalised here to an ISO ``YYYY-MM-DD`` string rather
    than passed through as published. An earlier revision deliberately kept
    the raw string, reasoning that facts assembly parses every other source's
    dates anyway and a second parsing path would only be somewhere for a
    format assumption to drift. The format drifted regardless — 2016/17 is
    published with two-digit years and every later season with four — and
    because the drift was silent rather than loud (see :func:`_to_iso_date`),
    keeping the quirk unresolved is what let it reach the facts layer. It is
    quarantined at staging now, which is where the framework says
    source-specific quirks belong.
    """
    raw = decode_csv(body, MATCHES_AND_ODDS_SPEC.encoding)
    staged, report = stage_frame(raw, MATCHES_AND_ODDS_SPEC)
    staged = staged.with_columns(_to_iso_date("match_date"))
    staged = staged.with_columns(pl.lit(str(season)).alias("season")).select(
        ["season", *staged.columns]
    )
    return StagedMatchesAndOdds(frame=staged, report=report)
