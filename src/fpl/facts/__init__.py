"""Canonical player-fixture facts, assembled from staged tables (spec §5)."""

from fpl.facts.player_fixture import (
    KEY,
    FactsResult,
    build_player_fixture_facts,
    write_player_fixture_facts,
)
from fpl.facts.points import (
    RULESETS,
    PointsResult,
    build_points,
    ruleset_for_name,
    write_points,
)

__all__ = [
    "KEY",
    "RULESETS",
    "FactsResult",
    "PointsResult",
    "build_player_fixture_facts",
    "build_points",
    "ruleset_for_name",
    "write_player_fixture_facts",
    "write_points",
]
