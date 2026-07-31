from __future__ import annotations

import pytest

from fpl.scoring.base import (
    PlayerFixtureRow,
    PointsBreakdown,
    appearance_points,
    card_points,
    clean_sheet_points,
    defensive_contribution_points,
    goal_points,
    goals_conceded_points,
    is_clean_sheet,
    own_goal_points,
    penalty_miss_points,
    penalty_save_points,
    save_points,
)


def _row(**overrides: object) -> PlayerFixtureRow:
    defaults: dict[str, object] = dict(
        position="MID",
        minutes=90,
        goals_scored=0,
        assists=0,
        goals_conceded=0,
        own_goals=0,
        penalties_saved=0,
        penalties_missed=0,
        yellow_cards=0,
        red_cards=0,
        saves=0,
        bonus=0,
    )
    defaults.update(overrides)
    return PlayerFixtureRow(**defaults)  # type: ignore[arg-type]


class TestPlayerFixtureRowValidation:
    def test_unknown_position_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown position"):
            _row(position="AM")

    def test_negative_minutes_raises(self) -> None:
        with pytest.raises(ValueError, match="minutes"):
            _row(minutes=-1)


class TestAppearancePoints:
    @pytest.mark.parametrize(
        ("minutes", "expected"),
        [(0, 0), (1, 1), (59, 1), (60, 2), (90, 2)],
    )
    def test_boundaries(self, minutes: int, expected: int) -> None:
        assert appearance_points(minutes) == expected


class TestGoalPoints:
    @pytest.mark.parametrize(
        ("position", "expected"),
        [("GK", 10), ("DEF", 6), ("MID", 5), ("FWD", 4)],
    )
    def test_per_position(self, position: str, expected: int) -> None:
        assert goal_points(position, 1) == expected  # type: ignore[arg-type]

    def test_multiple_goals(self) -> None:
        assert goal_points("FWD", 3) == 12


class TestCleanSheet:
    @pytest.mark.parametrize(
        ("minutes", "conceded", "expected"),
        [(60, 0, True), (59, 0, False), (90, 1, False), (120, 0, True)],
    )
    def test_is_clean_sheet(self, minutes: int, conceded: int, expected: bool) -> None:
        assert is_clean_sheet(minutes, conceded) is expected

    @pytest.mark.parametrize(
        ("position", "expected"),
        [("GK", 4), ("DEF", 4), ("MID", 1), ("FWD", 0)],
    )
    def test_points_per_position(self, position: str, expected: int) -> None:
        assert clean_sheet_points(position, 90, 0) == expected  # type: ignore[arg-type]

    def test_no_points_when_not_clean(self) -> None:
        assert clean_sheet_points("DEF", 90, 1) == 0

    def test_substitution_before_a_goal_still_earns_clean_sheet(self) -> None:
        """The spec's named case: a player subbed off at 60' before their team
        concedes later keeps the clean sheet, because goals_conceded here only
        counts goals conceded while this player was on the pitch — a goal
        conceded after they left never appears in this row's input at all."""
        assert clean_sheet_points("DEF", 60, 0) == 4


class TestGoalsConcededPoints:
    def test_gk_and_def_lose_a_point_per_two_conceded(self) -> None:
        assert goals_conceded_points("GK", 90, 2) == -1
        assert goals_conceded_points("DEF", 90, 4) == -2

    def test_integer_division_not_rounding(self) -> None:
        assert goals_conceded_points("GK", 90, 3) == -1

    def test_mid_and_fwd_never_lose_points(self) -> None:
        assert goals_conceded_points("MID", 90, 4) == 0
        assert goals_conceded_points("FWD", 90, 4) == 0

    def test_no_deduction_if_never_played(self) -> None:
        assert goals_conceded_points("DEF", 0, 4) == 0


class TestSavePoints:
    @pytest.mark.parametrize(("saves", "expected"), [(0, 0), (2, 0), (3, 1), (5, 1), (6, 2)])
    def test_integer_division(self, saves: int, expected: int) -> None:
        assert save_points(saves) == expected


class TestPenaltyPoints:
    def test_save_and_miss_are_independent(self) -> None:
        assert penalty_save_points(1) == 5
        assert penalty_miss_points(1) == -2
        assert penalty_save_points(1) + penalty_miss_points(1) == 3


class TestCardPoints:
    def test_yellow_and_red_independent(self) -> None:
        assert card_points(1, 0) == (-1, 0)
        assert card_points(0, 1) == (0, -3)
        assert card_points(1, 1) == (-1, -3)


class TestOwnGoalPoints:
    def test_own_goal_is_minus_two(self) -> None:
        assert own_goal_points(1) == -2
        assert own_goal_points(2) == -4


class TestDefensiveContributionPoints:
    def test_defender_threshold_inclusive(self) -> None:
        assert defensive_contribution_points("DEF", cbi=9, tackles=1, recoveries=0) == 2

    def test_defender_threshold_exclusive(self) -> None:
        assert defensive_contribution_points("DEF", cbi=9, tackles=0, recoveries=0) == 0

    def test_defender_does_not_stack(self) -> None:
        assert defensive_contribution_points("DEF", cbi=20, tackles=0, recoveries=0) == 2

    def test_defender_recoveries_do_not_count(self) -> None:
        assert defensive_contribution_points("DEF", cbi=6, tackles=3, recoveries=20) == 0

    def test_midfielder_threshold_inclusive_with_recoveries(self) -> None:
        assert defensive_contribution_points("MID", cbi=4, tackles=4, recoveries=4) == 2

    def test_midfielder_threshold_exclusive(self) -> None:
        assert defensive_contribution_points("MID", cbi=4, tackles=4, recoveries=3) == 0

    def test_forward_same_rule_as_midfielder(self) -> None:
        assert defensive_contribution_points("FWD", cbi=6, tackles=3, recoveries=3) == 2

    def test_goalkeeper_never_qualifies(self) -> None:
        assert defensive_contribution_points("GK", cbi=50, tackles=50, recoveries=50) == 0


class TestPointsBreakdownTotal:
    def test_total_sums_every_term(self) -> None:
        breakdown = PointsBreakdown(
            appearance=2,
            goals=5,
            assists=3,
            clean_sheet=1,
            goals_conceded=0,
            saves=0,
            penalties_saved=0,
            penalties_missed=0,
            yellow_cards=-1,
            red_cards=0,
            own_goals=0,
            defensive_contribution=2,
            bonus=3,
        )
        assert breakdown.total == 2 + 5 + 3 + 1 - 1 + 2 + 3
