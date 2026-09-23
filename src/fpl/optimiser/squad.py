"""``fpl.optimiser.squad`` — the squad and starting-XI picker
(Phase D, `.github/context/subsystem3-close-and-mvp-site.md`).

:func:`pick_squad` is an *exact* 0/1 knapsack-style MILP (via
``scipy.optimize.milp``, HiGHS backend): it maximises total predicted
points subject to the budget, per-position quotas and per-club cap,
rather than approximating with a greedy heuristic - so it will spend the
full budget whenever doing so raises the total, instead of stopping at
the first affordable "good enough" pick per slot.
:func:`pick_starting_xi` is also exact: with the squad size fixed at 15,
the best-of-13-choose-10 outfield formation is small enough to search
exhaustively rather than approximate.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import combinations

import numpy as np
import polars as pl
from scipy.optimize import Bounds, LinearConstraint, milp

from fpl.optimiser.rules import (
    BUDGET_MILLIONS,
    MAX_PLAYERS_PER_CLUB,
    SQUAD_POSITION_QUOTAS,
    STARTING_XI_MIN_QUOTAS,
    STARTING_XI_SIZE,
)
from fpl.scoring.base import Position

__all__ = [
    "SquadPlayer",
    "SquadSelection",
    "StartingXISelection",
    "pick_squad",
    "pick_starting_xi",
]

_REQUIRED_COLUMNS = frozenset(
    {"player_id", "team_id", "position", "price", "predicted_total_points_fpl"}
)


@dataclass(frozen=True)
class SquadPlayer:
    """One squad member, with points already summed across every fixture
    in the predictions partition's horizon (a double gameweek gives one
    player two rows; both count)."""

    player_id: int
    team_id: int
    position: Position
    price: float
    predicted_points: float


@dataclass(frozen=True)
class SquadSelection:
    players: tuple[SquadPlayer, ...]

    @property
    def total_price(self) -> float:
        return sum(player.price for player in self.players)

    @property
    def total_predicted_points(self) -> float:
        return sum(player.predicted_points for player in self.players)


@dataclass(frozen=True)
class StartingXISelection:
    starting_xi: tuple[SquadPlayer, ...]
    bench: tuple[SquadPlayer, ...]
    captain: SquadPlayer
    vice_captain: SquadPlayer


def _candidates(predictions: pl.DataFrame) -> list[SquadPlayer]:
    missing = _REQUIRED_COLUMNS - set(predictions.columns)
    if missing:
        raise ValueError(f"predictions frame is missing required column(s): {sorted(missing)}")

    aggregated = (
        predictions.filter(pl.col("predicted_total_points_fpl").is_not_null())
        .group_by("player_id", maintain_order=True)
        .agg(
            pl.col("team_id").first(),
            pl.col("position").first(),
            pl.col("price").first(),
            pl.col("predicted_total_points_fpl").sum().alias("predicted_points"),
        )
    )
    return [
        SquadPlayer(
            player_id=row["player_id"],
            team_id=row["team_id"],
            position=row["position"],
            price=float(row["price"]),
            predicted_points=float(row["predicted_points"]),
        )
        for row in aggregated.iter_rows(named=True)
    ]


def _solve_milp(candidates: list[SquadPlayer], budget: float) -> list[SquadPlayer] | None:
    """Exact 0/1 selection maximising total predicted points subject to
    budget, per-position quotas and the per-club cap. Returns ``None`` if
    the constraints admit no feasible squad (scipy's ``milp`` reports
    non-success), rather than raising - the caller decides how to report
    that."""
    points = np.array([candidate.predicted_points for candidate in candidates])
    prices = np.array([candidate.price for candidate in candidates])

    constraints = [LinearConstraint(prices, -np.inf, budget)]
    for position, quota in SQUAD_POSITION_QUOTAS.items():
        row = np.array([1.0 if c.position == position else 0.0 for c in candidates])
        constraints.append(LinearConstraint(row, quota, quota))
    for club in sorted({c.team_id for c in candidates}):
        row = np.array([1.0 if c.team_id == club else 0.0 for c in candidates])
        constraints.append(LinearConstraint(row, -np.inf, MAX_PLAYERS_PER_CLUB))

    result = milp(
        c=-points,
        constraints=constraints,
        integrality=np.ones(len(candidates)),
        bounds=Bounds(0, 1),
    )
    if not result.success:
        return None
    return [
        candidate for candidate, chosen in zip(candidates, result.x > 0.5, strict=True) if chosen
    ]


def pick_squad(predictions: pl.DataFrame, *, budget: float = BUDGET_MILLIONS) -> SquadSelection:
    """Exact squad selection: the combination of candidates that maximises
    total predicted points subject to the budget, each position's quota,
    and the per-club cap (a 0/1 knapsack-style MILP, see module
    docstring)."""
    candidates = _candidates(predictions)

    # Pre-flight per-position feasibility check, so a shortage is reported
    # against the specific position responsible rather than as scipy's
    # generic "infeasible" MILP status.
    counts = Counter(candidate.position for candidate in candidates)
    for position, quota in SQUAD_POSITION_QUOTAS.items():
        available = counts.get(position, 0)
        if available < quota:
            raise ValueError(
                f"cannot complete squad: only {available} {position} candidate(s) available, "
                f"need {quota}"
            )

    picked = _solve_milp(candidates, budget)
    if picked is None:
        raise ValueError(
            "cannot complete squad: no combination of candidates satisfies the budget, "
            "position and per-club constraints together"
        )

    return SquadSelection(players=tuple(picked))


def pick_starting_xi(squad: SquadSelection) -> StartingXISelection:
    """Best-scoring valid formation from the 15-man squad (1 GK, at least
    3 DEF, at least 1 FWD, 11 total - `docs/Fantasy Premier League
    Rules.md` §Choosing Your Starting Eleven), captain/vice-captain the
    top two predicted scorers within that XI, bench ordered goalkeeper
    first (mirroring FPL's own automatic-substitution priority) then by
    descending predicted points.

    Exact, not heuristic: with only 13 outfield players competing for 10
    XI slots, every ``13 choose 10`` combination (286) is checked."""
    goalkeepers = [player for player in squad.players if player.position == "GK"]
    outfield = [player for player in squad.players if player.position != "GK"]
    expected_goalkeepers = SQUAD_POSITION_QUOTAS["GK"]
    if len(goalkeepers) != expected_goalkeepers:
        raise ValueError(
            f"squad must carry exactly {expected_goalkeepers} goalkeeper(s), got {len(goalkeepers)}"
        )

    starting_gk = max(goalkeepers, key=lambda player: player.predicted_points)
    bench_gk = next(player for player in goalkeepers if player.player_id != starting_gk.player_id)

    outfield_xi_size = STARTING_XI_SIZE - STARTING_XI_MIN_QUOTAS["GK"]
    min_def = STARTING_XI_MIN_QUOTAS["DEF"]
    min_fwd = STARTING_XI_MIN_QUOTAS["FWD"]

    best_combo: tuple[SquadPlayer, ...] | None = None
    best_points = float("-inf")
    for combo in combinations(outfield, outfield_xi_size):
        def_count = sum(1 for player in combo if player.position == "DEF")
        fwd_count = sum(1 for player in combo if player.position == "FWD")
        if def_count < min_def or fwd_count < min_fwd:
            continue
        points = sum(player.predicted_points for player in combo)
        if points > best_points:
            best_points = points
            best_combo = combo

    if best_combo is None:
        raise ValueError("no valid starting XI formation satisfies the minimum DEF/FWD quotas")

    starting_ids = {player.player_id for player in best_combo}
    bench_outfield = [player for player in outfield if player.player_id not in starting_ids]
    bench_outfield.sort(key=lambda player: player.predicted_points, reverse=True)

    starting_xi = (starting_gk, *best_combo)
    ranked_by_points = sorted(starting_xi, key=lambda player: player.predicted_points, reverse=True)
    captain, vice_captain = ranked_by_points[0], ranked_by_points[1]

    return StartingXISelection(
        starting_xi=starting_xi,
        bench=(bench_gk, *bench_outfield),
        captain=captain,
        vice_captain=vice_captain,
    )
