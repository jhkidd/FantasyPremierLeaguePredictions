"""Scoring for 2025/26 — adds the defensive-contribution term (Finding 8).

+2 points, never stacking: defenders at CBI + tackles >= 10; midfielders and
forwards at CBI + tackles + recoveries >= 12; goalkeepers never qualify.
Everything else is identical to ``rules_legacy``.
"""

from __future__ import annotations

from dataclasses import dataclass

from fpl.scoring.base import (
    PlayerFixtureRow,
    PointsBreakdown,
    base_breakdown,
    defensive_contribution_points,
)

NAME = "2025-26"


@dataclass(frozen=True)
class Rules202526:
    name: str = NAME

    def points(self, row: PlayerFixtureRow) -> PointsBreakdown:
        dc = defensive_contribution_points(row.position, row.cbi, row.tackles, row.recoveries)
        return base_breakdown(row, defensive_contribution=dc)


__all__ = ["NAME", "Rules202526"]
