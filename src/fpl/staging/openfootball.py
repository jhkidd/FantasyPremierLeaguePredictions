"""Stage `openfootball/champions-league` fixture-list files into one table.

Every extracted file (``cl.txt``, ``clq.txt``, ``elq.txt``, ``confq.txt``)
shares the same ``football.txt`` format and the same output shape — only
which competition a given file belongs to differs, and that is supplied by
the caller (from :data:`fpl.sources.openfootball.SEASON_FILES`'s endpoint
name), not parsed from the text itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import polars as pl

from fpl.config import Season
from fpl.staging.base import ColumnSpec, StagingReport, TableSpec, stage_frame
from fpl.staging.openfootball_parser import parse_football_txt

__all__ = ["FIXTURES_SPEC", "StagedFixtures", "stage_fixtures"]

FIXTURES_SPEC = TableSpec(
    table="openfootball_fixtures",
    key=("competition", "match_date", "home_team", "away_team"),
    columns=(
        ColumnSpec("match_date", "match_date", pl.Date),
        ColumnSpec("round", "round", pl.Utf8),
        ColumnSpec("home_team", "home_team", pl.Utf8),
        ColumnSpec("home_country", "home_country", pl.Utf8),
        ColumnSpec("away_team", "away_team", pl.Utf8),
        ColumnSpec("away_country", "away_country", pl.Utf8),
        ColumnSpec("competition", "competition", pl.Utf8),
    ),
)


@dataclass(frozen=True)
class StagedFixtures:
    frame: pl.DataFrame
    report: StagingReport


def stage_fixtures(body: bytes, season: Season, competition: str) -> StagedFixtures:
    """Stage one extracted file's fixtures.

    ``competition`` is the endpoint name the file was fetched under (e.g.
    ``"champions_league"``) - a `football.txt` document's own title line
    names the tournament in prose ("UEFA Champions League 2025/26") but not
    in a form worth parsing back out, since the caller already knows exactly
    which of the four tracked files it is staging.
    """
    fixtures = parse_football_txt(body.decode("utf-8"), source_label=competition)
    rows = [{**asdict(fixture), "competition": competition} for fixture in fixtures]
    raw = pl.DataFrame(
        rows,
        schema={
            "match_date": pl.Date,
            "round": pl.Utf8,
            "home_team": pl.Utf8,
            "home_country": pl.Utf8,
            "away_team": pl.Utf8,
            "away_country": pl.Utf8,
            "competition": pl.Utf8,
        },
    )
    staged, report = stage_frame(raw, FIXTURES_SPEC)
    staged = staged.with_columns(pl.lit(str(season)).alias("season")).select(
        ["season", *staged.columns]
    )
    return StagedFixtures(frame=staged, report=report)
