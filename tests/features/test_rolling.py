from __future__ import annotations

import polars as pl

from fpl.features.rolling import build_rolling_features

_BASE_ROW = {
    "minutes": 90,
    "starts": 1,
    "goals_scored": 0,
    "assists": 0,
    "goals_conceded": 1,
    "own_goals": 0,
    "penalties_saved": 0,
    "penalties_missed": 0,
    "yellow_cards": 0,
    "red_cards": 0,
    "saves": 0,
    "cbi": 2,
    "tackles": 1,
    "recoveries": 3,
    "defensive_contribution": 6,
    "attempted_passes": 20,
    "completed_passes": 18,
    "key_passes": 1,
    "big_chances_created": 0,
    "big_chances_missed": 0,
    "open_play_crosses": 0,
    "dribbles": 1,
    "tackled": 0,
    "fouls": 0,
    "offside": 0,
    "target_missed": 0,
    "errors_leading_to_goal": 0,
    "errors_leading_to_goal_attempt": 0,
    "penalties_conceded": 0,
    "winning_goals": 0,
    "expected_goals": 0.1,
    "expected_assists": 0.05,
    "expected_goal_involvements": 0.15,
    "expected_goals_conceded": 1.0,
    "obs_defensive": True,
    "obs_bps_inputs": True,
    "obs_expected": True,
}


def _row(**overrides: object) -> dict:
    row = dict(_BASE_ROW)
    row.update(overrides)
    return row


def _frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows)


class TestUnmaskedWindows:
    def test_sum_and_per90_over_last_n_fixtures(self) -> None:
        # 4 fixtures, each 90 minutes, goals_scored = 1,0,0,2 (oldest first).
        rows = [
            _row(goals_scored=1),
            _row(goals_scored=0),
            _row(goals_scored=0),
            _row(goals_scored=2),
        ]
        features = build_rolling_features(_frame(rows))

        # last 3 -> goals 0,0,2 = 2 over 270 minutes -> per90 = 2/270*90
        assert features["goals_scored_sum_last_3"] == 2
        assert features["goals_scored_per90_last_3"] == 2 / 270 * 90

    def test_window_larger_than_history_uses_all_available(self) -> None:
        rows = [_row(goals_scored=1), _row(goals_scored=1)]
        features = build_rolling_features(_frame(rows))

        assert features["goals_scored_sum_last_10"] == 2
        assert features["goals_scored_per90_last_10"] == 2 / 180 * 90

    def test_empty_history_returns_nulls(self) -> None:
        features = build_rolling_features(_frame([]))

        assert features["goals_scored_sum_last_3"] is None
        assert features["goals_scored_per90_last_3"] is None
        assert features["goals_scored_sum_last_season_to_date"] is None
        assert features["goals_scored_sum_last_last_season"] is None


class TestSeasonToDate:
    def test_season_to_date_is_distinct_from_all_history(self) -> None:
        # `history` spans a season boundary (2 fixtures last season, 1 this
        # season); season_to_date_history is only this season's 1 fixture.
        history = _frame(
            [
                _row(goals_scored=5),
                _row(goals_scored=5),
                _row(goals_scored=1),
            ]
        )
        season_to_date = _frame([_row(goals_scored=1)])

        features = build_rolling_features(history, season_to_date_history=season_to_date)

        # season-to-date must reflect only the 1 fixture this season, not
        # all 3 fixtures of `history`.
        assert features["goals_scored_sum_last_season_to_date"] == 1
        # whereas the fixed fixture-count window still spans the boundary.
        assert features["goals_scored_sum_last_3"] == 11

    def test_no_season_to_date_history_yields_nulls(self) -> None:
        history = _frame([_row(goals_scored=5)])
        features = build_rolling_features(history, season_to_date_history=None)

        assert features["goals_scored_sum_last_season_to_date"] is None
        assert features["goals_scored_per90_last_season_to_date"] is None


class TestLastSeason:
    def test_last_season_aggregates_partial_history_as_is(self) -> None:
        history = _frame([_row(goals_scored=1)])
        last_season = _frame([_row(goals_scored=3), _row(goals_scored=4)])

        features = build_rolling_features(history, last_season_history=last_season)

        assert features["goals_scored_sum_last_last_season"] == 7
        assert features["goals_scored_per90_last_last_season"] == 7 / 180 * 90

    def test_no_last_season_history_yields_nulls(self) -> None:
        history = _frame([_row(goals_scored=1)])
        features = build_rolling_features(history, last_season_history=None)

        assert features["goals_scored_sum_last_last_season"] is None
        assert features["goals_scored_per90_last_last_season"] is None


class TestMaskedColumns:
    def test_masked_window_excludes_unobserved_fixtures_and_counts_them(self) -> None:
        # Last 3 fixtures: 2 with defensive stats observed, 1 without
        # (an era-boundary gap) - the unobserved fixture must not
        # contribute a silent zero, and must be counted.
        rows = [
            _row(tackles=2, obs_defensive=True),
            _row(tackles=99, obs_defensive=False),
            _row(tackles=3, obs_defensive=True),
        ]
        features = build_rolling_features(_frame(rows))

        assert features["tackles_sum_last_3"] == 5
        assert features["tackles_masked_count_last_3"] == 1
        # per90 uses only the observed fixtures' minutes (2 * 90 = 180).
        assert features["tackles_per90_last_3"] == 5 / 180 * 90

    def test_all_unobserved_in_window_yields_null_sum_and_full_masked_count(self) -> None:
        rows = [_row(tackles=2, obs_defensive=False) for _ in range(3)]
        features = build_rolling_features(_frame(rows))

        assert features["tackles_sum_last_3"] is None
        assert features["tackles_per90_last_3"] is None
        assert features["tackles_masked_count_last_3"] == 3

    def test_masked_season_to_date_and_last_season(self) -> None:
        season_to_date = _frame([_row(cbi=1, obs_defensive=True), _row(cbi=5, obs_defensive=False)])
        last_season = _frame([_row(cbi=2, obs_defensive=True), _row(cbi=2, obs_defensive=True)])

        features = build_rolling_features(
            _frame([]),
            season_to_date_history=season_to_date,
            last_season_history=last_season,
        )

        assert features["cbi_sum_last_season_to_date"] == 1
        assert features["cbi_masked_count_last_season_to_date"] == 1
        assert features["cbi_sum_last_last_season"] == 4
        assert features["cbi_masked_count_last_last_season"] == 0

    def test_empty_history_masked_columns_default_masked_count_zero(self) -> None:
        features = build_rolling_features(_frame([]))

        assert features["tackles_sum_last_3"] is None
        assert features["tackles_masked_count_last_3"] == 0
        assert features["tackles_masked_count_last_season_to_date"] == 0
        assert features["tackles_masked_count_last_last_season"] == 0


class TestFeatureKeysArePresent:
    def test_all_expected_keys_present_for_empty_history(self) -> None:
        features = build_rolling_features(_frame([]))

        for label in ("3", "5", "10", "season_to_date", "last_season"):
            assert f"goals_scored_sum_last_{label}" in features
            assert f"goals_scored_per90_last_{label}" in features
            assert f"tackles_sum_last_{label}" in features
            assert f"tackles_masked_count_last_{label}" in features
