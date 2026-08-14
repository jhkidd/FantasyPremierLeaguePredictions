"""Reconstruct a season's ``fixtures`` table from staged player-fixture stats.

Vaastav's archive publishes ``fixtures.csv`` from 2018/19 onward only, so the
two earliest seasons have no fixture list at all — and the FPL API serves the
*current* season exclusively, so it cannot supply one retrospectively either.
Without this, ``facts/team_fixture`` (and therefore every team-context
feature: elo, congestion, odds) simply cannot be built for those seasons.

Everything the table needs is already implied by the per-player rows. The two
teams of a fixture are its two distinct ``opponent_team_id`` values (the same
invariant ``facts/player_fixture`` uses to derive ``team_id``), and ``was_home``
says which way round they go. Scores come from the ``team_h_score`` /
``team_a_score`` columns the archive repeats on every player row.

``code`` — FPL's globally unique cross-season fixture id — is genuinely
unavailable here and is written null rather than invented.
"""

from __future__ import annotations

import polars as pl

from fpl.config import Season
from fpl.staging.base import StagingReport

__all__ = ["FIXTURE_COLUMNS", "fixtures_from_player_stats"]

FIXTURE_COLUMNS: tuple[str, ...] = (
    "season",
    "fixture_id",
    "code",
    "event",
    "kickoff_time",
    "team_h",
    "team_a",
    "team_h_score",
    "team_a_score",
    "finished",
    "minutes",
)
"""Column order of :data:`fpl.staging.fpl_api.FIXTURES_SPEC`, matched exactly
so a reconstructed season is indistinguishable downstream from a staged one."""


def _home_away_ids(stats: pl.DataFrame) -> pl.DataFrame:
    """One row per fixture giving ``team_h`` / ``team_a``.

    A home player's ``opponent_team_id`` *is* the away team, and vice versa —
    so the two ids fall out of the opponent column alone, without needing a
    ``team_id`` to have been resolved first.
    """
    home = (
        stats.filter(pl.col("was_home"))
        .group_by("fixture_id")
        .agg(pl.col("opponent_team_id").drop_nulls().first().alias("team_a"))
    )
    away = (
        stats.filter(~pl.col("was_home"))
        .group_by("fixture_id")
        .agg(pl.col("opponent_team_id").drop_nulls().first().alias("team_h"))
    )
    return home.join(away, on="fixture_id", how="full", coalesce=True)


def fixtures_from_player_stats(
    stats: pl.DataFrame, season: Season
) -> tuple[pl.DataFrame, StagingReport]:
    """Derive one ``fixtures`` row per distinct ``fixture_id`` in ``stats``.

    ``stats`` is a staged ``player_fixture_stats`` frame, which still names
    the opponent column ``opponent_team`` (facts assembly is what renames it);
    both spellings are accepted so this works either side of that rename.
    """
    if "opponent_team" in stats.columns and "opponent_team_id" not in stats.columns:
        stats = stats.rename({"opponent_team": "opponent_team_id"})

    rows_in = stats.height
    scores = {"team_h_score", "team_a_score"} & set(stats.columns)
    aggregations = [
        pl.col("event").drop_nulls().first().alias("event"),
        pl.col("kickoff_time").drop_nulls().first().alias("kickoff_time"),
        pl.col("minutes").max().alias("minutes"),
    ]
    aggregations += [pl.col(name).drop_nulls().first().alias(name) for name in sorted(scores)]

    per_fixture = stats.group_by("fixture_id").agg(aggregations)
    fixtures = per_fixture.join(_home_away_ids(stats), on="fixture_id", how="left")

    for name in ("team_h_score", "team_a_score"):
        if name not in fixtures.columns:
            fixtures = fixtures.with_columns(pl.lit(None, dtype=pl.Int64).alias(name))

    fixtures = fixtures.with_columns(
        pl.lit(str(season)).alias("season"),
        pl.lit(None, dtype=pl.Int64).alias("code"),
        # Only played fixtures generate player rows at all, so every fixture
        # reconstructable this way is by definition finished.
        pl.lit(True).alias("finished"),
        pl.col("fixture_id").cast(pl.Int64),
        pl.col("event").cast(pl.Int64),
        pl.col("kickoff_time").cast(pl.Utf8),
        pl.col("team_h").cast(pl.Int64),
        pl.col("team_a").cast(pl.Int64),
        pl.col("team_h_score").cast(pl.Int64),
        pl.col("team_a_score").cast(pl.Int64),
        pl.col("minutes").cast(pl.Int64),
    ).select(list(FIXTURE_COLUMNS))

    report = StagingReport(
        table="fixtures",
        rows_in=rows_in,
        rows_out=fixtures.height,
        unknown_columns=(),
        excluded={},
    )
    return fixtures, report
