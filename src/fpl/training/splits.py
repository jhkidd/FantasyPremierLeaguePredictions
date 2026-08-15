"""Chronological train/validation/test split for the training matrix (Split
B, plan Phase A Step 24).

A random row-level split would leak information: a player's rolling features
in one row are partly derived from their own fixtures in nearby rows, so a
split that mixes rows from the same season (or worse, the same gameweek)
across train/validation/test lets the model implicitly see its own targets
from a neighbouring row. Splitting whole seasons apart avoids this entirely -
every row in a season depends only on that season's own earlier fixtures
(plus, for one aggregate, the single prior season), never a *later* one, so
holding an entire season back is a genuine temporal holdout.

Split B fixes validation at the season immediately before the current one
and test at the most recently completed season, leaving every earlier
backfilled season for training - the standard "old data trains, next-to-last
tunes, last proves" arrangement for a season-boundary time series.
"""

from __future__ import annotations

import polars as pl

from fpl.config import Season

__all__ = ["TEST_SEASON", "TRAIN_SEASONS", "VALIDATION_SEASON", "chronological_split"]

_TRAIN_START_YEAR = 2016
_TRAIN_END_YEAR = 2023

TRAIN_SEASONS: tuple[str, ...] = tuple(
    str(Season(year)) for year in range(_TRAIN_START_YEAR, _TRAIN_END_YEAR + 1)
)
VALIDATION_SEASON: str = str(Season(2024))
TEST_SEASON: str = str(Season(2025))


def chronological_split(frame: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Partition ``frame`` by its ``season`` column into
    ``(train, validation, test)``, per Split B's fixed season boundaries.

    Raises ``ValueError`` if any row's ``season`` falls outside the ten
    backfilled seasons Split B knows about - silently dropping such a row
    would make the partitions non-exhaustive without any visible sign that
    data went missing.
    """
    known_seasons = {*TRAIN_SEASONS, VALIDATION_SEASON, TEST_SEASON}
    observed_seasons = set(frame["season"].unique().to_list())
    unknown = sorted(observed_seasons - known_seasons)
    if unknown:
        raise ValueError(
            f"chronological_split: season(s) outside Split B's known range "
            f"({TRAIN_SEASONS[0]}..{TEST_SEASON}): {unknown}"
        )

    train = frame.filter(pl.col("season").is_in(TRAIN_SEASONS))
    validation = frame.filter(pl.col("season") == VALIDATION_SEASON)
    test = frame.filter(pl.col("season") == TEST_SEASON)
    return train, validation, test
