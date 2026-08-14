"""Scoring framework: the itemised, position-aware arithmetic FPL applies to a
single player-fixture performance (spec §6).

Every ruleset expresses its arithmetic in terms of a :class:`PlayerFixtureRow`
and returns an itemised :class:`PointsBreakdown` — never a bare total. When
reconciliation fails on thousands of rows, the breakdown is what turns "we are
2 points out" into "our clean-sheet term is wrong for substitutes".

Nothing here knows about parquet, paths, or seasons. A ruleset's ``points()``
is a pure function of one row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Position = Literal["GK", "DEF", "MID", "FWD"]

POSITIONS: frozenset[Position] = frozenset({"GK", "DEF", "MID", "FWD"})


@dataclass(frozen=True)
class PlayerFixtureRow:
    """The minimal set of observed inputs any ruleset needs to score one
    player's performance in one fixture.

    Deliberately independent of the facts table's column set — this is the
    scoring domain's own vocabulary. A facts-assembly step (phase 5.4) maps
    staged columns onto this shape; it does not the other way around.
    """

    position: Position
    minutes: int
    goals_scored: int
    assists: int
    goals_conceded: int
    own_goals: int
    penalties_saved: int
    penalties_missed: int
    yellow_cards: int
    red_cards: int
    saves: int
    bonus: int
    """Observed, passed through as FPL's published value — never derived
    (Finding 2). Reconciliation must be exact, so bonus is data, not a rule."""

    cbi: int = 0
    """Clearances, blocks, interceptions. 0 (not null) where genuinely absent
    in this ruleset's era — the facts layer is what carries the null/mask
    distinction; scoring only ever sees an era where the term applies."""

    tackles: int = 0
    recoveries: int = 0

    def __post_init__(self) -> None:
        if self.position not in POSITIONS:
            raise ValueError(f"unknown position {self.position!r}; expected one of {POSITIONS}")
        if self.minutes < 0:
            raise ValueError(f"minutes must not be negative: {self.minutes}")


@dataclass(frozen=True)
class PointsBreakdown:
    """FPL's points, itemised by term. Never collapse this to a total before
    a caller has had the chance to inspect the terms — see module docstring.
    """

    appearance: int
    goals: int
    assists: int
    clean_sheet: int
    goals_conceded: int
    saves: int
    penalties_saved: int
    penalties_missed: int
    yellow_cards: int
    red_cards: int
    own_goals: int
    defensive_contribution: int
    bonus: int

    @property
    def total(self) -> int:
        return (
            self.appearance
            + self.goals
            + self.assists
            + self.clean_sheet
            + self.goals_conceded
            + self.saves
            + self.penalties_saved
            + self.penalties_missed
            + self.yellow_cards
            + self.red_cards
            + self.own_goals
            + self.defensive_contribution
            + self.bonus
        )


class Rules(Protocol):
    """A named, pure scoring function for one era's arithmetic."""

    name: str

    def points(self, row: PlayerFixtureRow) -> PointsBreakdown: ...


# --- Shared arithmetic, common to every ruleset seen so far -----------------
#
# The only points-affecting change across ten seasons is defensive
# contribution in 2025/26 onward (see rules_2025_26.py). Everything below is
# identical in every ruleset and lives here so a shared term can only be
# fixed, or broken, once.

_GOAL_POINTS: dict[Position, int] = {"GK": 6, "DEF": 6, "MID": 5, "FWD": 4}
_CLEAN_SHEET_POINTS: dict[Position, int] = {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0}


def appearance_points(minutes: int) -> int:
    if minutes <= 0:
        return 0
    return 2 if minutes >= 60 else 1


def goal_points(position: Position, goals_scored: int) -> int:
    return _GOAL_POINTS[position] * goals_scored


def assist_points(assists: int) -> int:
    return 3 * assists


def is_clean_sheet(minutes: int, goals_conceded: int) -> bool:
    """A clean sheet requires 60+ minutes and zero goals conceded while on the
    pitch. Derived, never read from FPL's own flag — reading it would make
    reconciliation circular (spec's locked decision)."""
    return minutes >= 60 and goals_conceded == 0


def clean_sheet_points(position: Position, minutes: int, goals_conceded: int) -> int:
    if not is_clean_sheet(minutes, goals_conceded):
        return 0
    return _CLEAN_SHEET_POINTS[position]


def goals_conceded_points(position: Position, minutes: int, goals_conceded: int) -> int:
    """−1 for every 2 goals conceded, GK and DEF only, and only while they
    were on the pitch at all."""
    if position not in {"GK", "DEF"} or minutes <= 0:
        return 0
    return -(goals_conceded // 2)


def save_points(saves: int) -> int:
    return saves // 3


def penalty_save_points(penalties_saved: int) -> int:
    return 5 * penalties_saved


def penalty_miss_points(penalties_missed: int) -> int:
    return -2 * penalties_missed


def card_points(yellow_cards: int, red_cards: int) -> tuple[int, int]:
    return -1 * yellow_cards, -3 * red_cards


def own_goal_points(own_goals: int) -> int:
    return -2 * own_goals


def defensive_contribution_points(
    position: Position, cbi: int, tackles: int, recoveries: int
) -> int:
    """+2 points, never stacking, on distinct thresholds by position group.

    Defenders: CBI + tackles >= 10. Recoveries do not count for them.
    Midfielders/forwards: CBI + tackles + recoveries >= 12.
    Goalkeepers never qualify.
    """
    if position == "DEF":
        return 2 if (cbi + tackles) >= 10 else 0
    if position in {"MID", "FWD"}:
        return 2 if (cbi + tackles + recoveries) >= 12 else 0
    return 0


def base_breakdown(row: PlayerFixtureRow, *, defensive_contribution: int = 0) -> PointsBreakdown:
    """Every term shared by every ruleset, assembled from one row.

    ``defensive_contribution`` is a parameter rather than always computed here
    because ``rules_legacy`` must not offer the term at all (Finding 5's
    2019/20 floor — no CBI/tackles/recoveries columns exist in most of those
    seasons, and where they do the rule did not exist yet)."""
    yellow, red = card_points(row.yellow_cards, row.red_cards)
    return PointsBreakdown(
        appearance=appearance_points(row.minutes),
        goals=goal_points(row.position, row.goals_scored),
        assists=assist_points(row.assists),
        clean_sheet=clean_sheet_points(row.position, row.minutes, row.goals_conceded),
        goals_conceded=goals_conceded_points(row.position, row.minutes, row.goals_conceded),
        saves=save_points(row.saves),
        penalties_saved=penalty_save_points(row.penalties_saved),
        penalties_missed=penalty_miss_points(row.penalties_missed),
        yellow_cards=yellow,
        red_cards=red,
        own_goals=own_goal_points(row.own_goals),
        defensive_contribution=defensive_contribution,
        bonus=row.bonus,
    )


__all__ = [
    "POSITIONS",
    "PlayerFixtureRow",
    "PointsBreakdown",
    "Position",
    "Rules",
    "appearance_points",
    "assist_points",
    "card_points",
    "clean_sheet_points",
    "defensive_contribution_points",
    "goal_points",
    "goals_conceded_points",
    "is_clean_sheet",
    "own_goal_points",
    "penalty_miss_points",
    "penalty_save_points",
    "save_points",
    "base_breakdown",
]
