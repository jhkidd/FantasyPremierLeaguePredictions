"""Tests for :mod:`fpl.optimiser.rules` (Phase D step 10,
`.github/context/subsystem3-close-and-mvp-site.md`) - pinned against
``docs/Fantasy Premier League Rules.md``."""

from __future__ import annotations

from fpl.optimiser.rules import (
    BUDGET_MILLIONS,
    MAX_PLAYERS_PER_CLUB,
    SQUAD_POSITION_QUOTAS,
    SQUAD_SIZE,
    STARTING_XI_MIN_QUOTAS,
    STARTING_XI_SIZE,
)


def test_squad_quotas_match_the_rules_doc() -> None:
    assert SQUAD_POSITION_QUOTAS == {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
    assert sum(SQUAD_POSITION_QUOTAS.values()) == SQUAD_SIZE == 15


def test_budget_matches_the_rules_doc() -> None:
    assert BUDGET_MILLIONS == 100.0


def test_max_players_per_club_matches_the_rules_doc() -> None:
    assert MAX_PLAYERS_PER_CLUB == 3


def test_starting_xi_minimums_match_the_rules_doc() -> None:
    assert STARTING_XI_SIZE == 11
    assert STARTING_XI_MIN_QUOTAS == {"GK": 1, "DEF": 3, "FWD": 1}
