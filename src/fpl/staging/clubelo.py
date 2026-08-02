"""Stage Club Elo's per-date ratings CSV into a typed table.

One shape only (unlike ``staging/vaastav.py``'s seven eras): Club Elo's
``Rank,Club,Country,Level,Elo,From,To`` header has been confirmed live and
carries no known historical schema drift. ``Club`` is the source-name column
the team crosswalk (plan §7.6) resolves against. ``Level`` is retained even
though only Premier League opponents ultimately matter — a full daily pull
also carries non-English clubs and lower-division English clubs, and
filtering to Premier League opponents happens at facts-assembly time (plan
§7.7), not here, so the staged table stays a faithful copy of what Club Elo
actually published.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

from fpl.config import Season
from fpl.staging.base import ColumnSpec, StagingReport, TableSpec, decode_csv, stage_frame

__all__ = ["RATINGS_SPEC", "StagedRatings", "stage_ratings"]

RATINGS_SPEC = TableSpec(
    # ``key`` describes this spec's own output, one day's pull, where "club"
    # alone is unique. ``stage_ratings`` below stamps "season"/"as_of_date"
    # onto the result afterwards - the real key of a table built by staging
    # many days is ("season", "as_of_date", "club").
    table="clubelo_ratings",
    key=("club",),
    columns=(
        ColumnSpec("rank", "Rank", pl.Int64),
        ColumnSpec("club", "Club", pl.Utf8),
        ColumnSpec("country", "Country", pl.Utf8),
        ColumnSpec("level", "Level", pl.Int64),
        ColumnSpec("elo", "Elo", pl.Float64),
        ColumnSpec("valid_from", "From", pl.Utf8),
        ColumnSpec("valid_to", "To", pl.Utf8),
    ),
)


@dataclass(frozen=True)
class StagedRatings:
    frame: pl.DataFrame
    report: StagingReport


def stage_ratings(body: bytes, as_of_date: date, season: Season) -> StagedRatings:
    """Stage one day's ratings pull into ``clubelo_ratings`` rows.

    ``as_of_date`` and ``season`` are stamped onto every row rather than
    inferred from ``valid_from``/``valid_to`` — those columns describe how
    long a rating stays current, not the date it was actually requested for,
    and the caller (facts assembly) already knows both values without
    needing this module to re-derive them.
    """
    raw = decode_csv(body, RATINGS_SPEC.encoding)
    staged, report = stage_frame(raw, RATINGS_SPEC)
    staged = staged.with_columns(
        pl.lit(as_of_date.isoformat()).alias("as_of_date"),
        pl.lit(str(season)).alias("season"),
    ).select(["season", "as_of_date", *staged.columns])
    return StagedRatings(frame=staged, report=report)
