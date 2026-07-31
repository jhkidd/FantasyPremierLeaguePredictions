"""Scoring for 2026/27.

Points arithmetic is identical to 2025/26 — the changes known for 2026/27 are
BPS-only (spec's locked decision), and bonus is always an observed passthrough
here rather than derived, so a BPS change alone does not change this module's
maths. It gets its own file anyway: a future points-affecting divergence
becomes a new file to write, not an edit to one two seasons already depend on.
"""

from __future__ import annotations

from dataclasses import dataclass

from fpl.scoring.base import (
    PlayerFixtureRow,
    PointsBreakdown,
    base_breakdown,
    defensive_contribution_points,
)

NAME = "2026-27"


@dataclass(frozen=True)
class Rules202627:
    name: str = NAME

    def points(self, row: PlayerFixtureRow) -> PointsBreakdown:
        dc = defensive_contribution_points(row.position, row.cbi, row.tackles, row.recoveries)
        return base_breakdown(row, defensive_contribution=dc)


__all__ = ["NAME", "Rules202627"]
