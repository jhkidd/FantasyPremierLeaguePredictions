"""Scoring for 2016/17 through 2024/25 — no defensive-contribution term.

Defensive contribution did not exist as a rule before 2025/26, and the input
columns it needs (CBI, tackles, recoveries) are absent for most of this span
(Finding 5's 2019/20 floor). This module never computes the term at all,
rather than computing it from zeros — a row that happens to carry zeros for
an unrelated reason must not silently earn the same treatment as a row where
the columns are genuinely absent.
"""

from __future__ import annotations

from dataclasses import dataclass

from fpl.scoring.base import PlayerFixtureRow, PointsBreakdown, base_breakdown

NAME = "legacy"


@dataclass(frozen=True)
class LegacyRules:
    name: str = NAME

    def points(self, row: PlayerFixtureRow) -> PointsBreakdown:
        return base_breakdown(row, defensive_contribution=0)


__all__ = ["NAME", "LegacyRules"]
