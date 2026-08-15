"""Tests for :mod:`fpl.training.splits` (Step 24, Split B)."""

from __future__ import annotations

import polars as pl
import pytest

from fpl.config import Season
from fpl.training.splits import TEST_SEASON, TRAIN_SEASONS, VALIDATION_SEASON, chronological_split


def _frame(seasons: list[str]) -> pl.DataFrame:
    return pl.DataFrame({"season": seasons, "player_id": list(range(len(seasons)))})


class TestChronologicalSplit:
    def test_boundaries_are_the_ten_backfilled_seasons(self) -> None:
        assert TRAIN_SEASONS == (
            "2016-17",
            "2017-18",
            "2018-19",
            "2019-20",
            "2020-21",
            "2021-22",
            "2022-23",
            "2023-24",
        )
        assert VALIDATION_SEASON == "2024-25"
        assert TEST_SEASON == "2025-26"

    def test_boundaries_are_strictly_ordered_in_time(self) -> None:
        last_train_year = Season.parse(TRAIN_SEASONS[-1]).start_year
        validation_year = Season.parse(VALIDATION_SEASON).start_year
        test_year = Season.parse(TEST_SEASON).start_year
        assert last_train_year < validation_year < test_year

    def test_partitions_are_exhaustive_and_disjoint(self) -> None:
        seasons = [*TRAIN_SEASONS, VALIDATION_SEASON, TEST_SEASON]
        frame = _frame(seasons)

        train, validation, test = chronological_split(frame)

        assert train.height + validation.height + test.height == frame.height
        assert set(train["season"].to_list()) == set(TRAIN_SEASONS)
        assert set(validation["season"].to_list()) == {VALIDATION_SEASON}
        assert set(test["season"].to_list()) == {TEST_SEASON}

    def test_last_train_season_never_leaks_into_validation(self) -> None:
        frame = _frame(["2023-24", "2024-25"])

        train, validation, _test = chronological_split(frame)

        assert train["season"].to_list() == ["2023-24"]
        assert validation["season"].to_list() == ["2024-25"]

    def test_unknown_season_raises(self) -> None:
        frame = _frame(["2016-17", "2026-27"])

        with pytest.raises(ValueError, match="2026-27"):
            chronological_split(frame)
