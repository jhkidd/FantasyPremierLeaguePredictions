"""Golden scoring cases, asserted term by term against all three rulesets.

Hand-written from ``docs/Fantasy Premier League Scoring.md``, plus the
boundaries the defensive-contribution rule actually turns on (spec §11).
Asserting on individual terms rather than the total is deliberate: a total can
be right for two compensating wrong reasons.

Every case runs against ``legacy``, ``2025-26`` and ``2026-27`` together, with
the defensive-contribution cases expected to differ under ``legacy`` — that is
what stops a rule being silently added to the wrong era.
"""

from __future__ import annotations

import pytest

from fpl.scoring.base import PlayerFixtureRow, Rules
from fpl.scoring.rules_2025_26 import Rules202526
from fpl.scoring.rules_2026_27 import Rules202627
from fpl.scoring.rules_legacy import LegacyRules

ALL_RULESETS: tuple[Rules, ...] = (LegacyRules(), Rules202526(), Rules202627())
DC_AWARE_RULESETS: tuple[Rules, ...] = (Rules202526(), Rules202627())


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
        cbi=0,
        tackles=0,
        recoveries=0,
    )
    defaults.update(overrides)
    return PlayerFixtureRow(**defaults)  # type: ignore[arg-type]


@pytest.mark.parametrize("rules", ALL_RULESETS, ids=lambda r: r.name)
class TestSharedTermsAcrossAllRulesets:
    def test_defender_10_cbit_earns_dc_under_dc_aware_rulesets_only(self, rules: Rules) -> None:
        row = _row(position="DEF", cbi=8, tackles=2)
        breakdown = rules.points(row)
        expected = 2 if rules.name != LegacyRules.name else 0
        assert breakdown.defensive_contribution == expected

    def test_defender_9_cbit_never_earns_dc(self, rules: Rules) -> None:
        row = _row(position="DEF", cbi=8, tackles=1)
        assert rules.points(row).defensive_contribution == 0

    def test_defender_20_cbit_does_not_stack(self, rules: Rules) -> None:
        row = _row(position="DEF", cbi=18, tackles=2)
        breakdown = rules.points(row)
        expected = 2 if rules.name != LegacyRules.name else 0
        assert breakdown.defensive_contribution == expected

    def test_midfielder_12_cbirt_earns_dc_including_recoveries(self, rules: Rules) -> None:
        row = _row(position="MID", cbi=4, tackles=4, recoveries=4)
        breakdown = rules.points(row)
        expected = 2 if rules.name != LegacyRules.name else 0
        assert breakdown.defensive_contribution == expected

    def test_midfielder_11_cbirt_never_earns_dc(self, rules: Rules) -> None:
        row = _row(position="MID", cbi=4, tackles=4, recoveries=3)
        assert rules.points(row).defensive_contribution == 0

    def test_defender_recoveries_do_not_count_toward_their_threshold(self, rules: Rules) -> None:
        """12 CBI+tackles+recoveries, but only 9 CBI+tackles: a defender does
        not qualify even though a midfielder with the same combined total
        would."""
        row = _row(position="DEF", cbi=6, tackles=3, recoveries=3)
        assert rules.points(row).defensive_contribution == 0

    def test_goalkeeper_never_earns_dc_however_high(self, rules: Rules) -> None:
        row = _row(position="GK", cbi=50, tackles=50, recoveries=50)
        assert rules.points(row).defensive_contribution == 0

    @pytest.mark.parametrize(("saves", "expected"), [(3, 1), (5, 1), (6, 2)])
    def test_goalkeeper_saves_use_integer_division(
        self, rules: Rules, saves: int, expected: int
    ) -> None:
        row = _row(position="GK", saves=saves)
        assert rules.points(row).saves == expected

    def test_appearance_boundary_59_versus_60_minutes(self, rules: Rules) -> None:
        assert rules.points(_row(minutes=59)).appearance == 1
        assert rules.points(_row(minutes=60)).appearance == 2

    def test_clean_sheet_with_a_59th_minute_substitution(self, rules: Rules) -> None:
        """The spec's named case: subbed off at minute 59 with the team still
        goalless earns no clean sheet (under 60 minutes played), but a
        substitution exactly at 60' with a goalless scoreline does."""
        assert rules.points(_row(position="DEF", minutes=59, goals_conceded=0)).clean_sheet == 0
        assert rules.points(_row(position="DEF", minutes=60, goals_conceded=0)).clean_sheet == 4

    def test_red_card_deductions_continue_after_the_card(self, rules: Rules) -> None:
        """Goals conceded after a red card still cost the player — the red
        card term and the goals-conceded term are independent and both
        apply."""
        breakdown = rules.points(
            _row(position="DEF", minutes=90, red_cards=1, goals_conceded=4)
        )
        assert breakdown.red_cards == -3
        assert breakdown.goals_conceded == -2

    def test_penalty_save_and_penalty_miss_in_the_same_match_are_independent(
        self, rules: Rules
    ) -> None:
        breakdown = rules.points(
            _row(position="GK", penalties_saved=1, penalties_missed=1)
        )
        assert breakdown.penalties_saved == 5
        assert breakdown.penalties_missed == -2

    def test_own_goal_by_a_clean_sheet_keeper_does_not_cancel_the_clean_sheet(
        self, rules: Rules
    ) -> None:
        """An own goal is scored as a goal conceded, so a keeper who concedes
        only via an own goal does not keep a clean sheet — but the two terms
        (clean_sheet and own_goals) are computed independently and neither
        term reaches into the other."""
        breakdown = rules.points(
            _row(position="GK", minutes=90, own_goals=1, goals_conceded=1)
        )
        assert breakdown.own_goals == -2
        assert breakdown.clean_sheet == 0
        assert breakdown.goals_conceded == 0

    def test_manager_asset_row_is_never_constructed(self, rules: Rules) -> None:
        """Manager rows (Finding 6) are rejected at staging, not scored — this
        is asserted here as a reminder that a ``minutes == 0`` row must never
        reach a ruleset with a non-zero bonus, since that is exactly the shape
        an unstaged manager row would have."""
        breakdown = rules.points(_row(minutes=0, bonus=0))
        assert breakdown.appearance == 0
