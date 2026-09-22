"""``fpl.optimiser.rules`` — squad-construction constants for the MVP
heuristic optimiser (Phase D, `.github/context/subsystem3-close-and-mvp-site.md`).

Sourced from ``docs/Fantasy Premier League Rules.md``, except
:data:`MAX_PLAYERS_PER_CLUB`, which is not documented anywhere in this
repo and is used here on general FPL knowledge (flagged in that plan's
Q&A for review).
"""

from __future__ import annotations

from fpl.scoring.base import Position

__all__ = [
    "BUDGET_MILLIONS",
    "MAX_PLAYERS_PER_CLUB",
    "SQUAD_POSITION_QUOTAS",
    "SQUAD_SIZE",
    "STARTING_XI_MIN_QUOTAS",
    "STARTING_XI_SIZE",
]

SQUAD_POSITION_QUOTAS: dict[Position, int] = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
"""§Squad Size: 15 players total, split across the four positions."""

SQUAD_SIZE: int = sum(SQUAD_POSITION_QUOTAS.values())

BUDGET_MILLIONS: float = 100.0
"""§Budget: total squad value must not exceed this."""

MAX_PLAYERS_PER_CLUB: int = 3
"""§Players Per Team. Not documented in this repo; general FPL knowledge."""

STARTING_XI_SIZE: int = 11
"""§Choosing Your Starting Eleven."""

STARTING_XI_MIN_QUOTAS: dict[Position, int] = {"GK": 1, "DEF": 3, "FWD": 1}
"""§Choosing Your Starting Eleven: "1 goalkeeper, at least 3 defenders and
at least 1 forward are selected at all times". Midfield has no stated
minimum, so it is absent here rather than pinned at zero."""
