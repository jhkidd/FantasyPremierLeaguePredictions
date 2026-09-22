"""``fpl.optimiser.squad`` — the MVP heuristic squad and starting-XI picker
(Phase D, `.github/context/subsystem3-close-and-mvp-site.md`).

:func:`pick_squad` is a greedy heuristic (best predicted-points-per-price
first, subject to budget/formation/club-limit constraints), explicitly
not a global optimum - a real MILP solver is deferred to a later task.
:func:`pick_starting_xi` *is* exact: with the squad size fixed at 15, the
best-of-13-choose-10 outfield formation is small enough to search
exhaustively rather than approximate.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import polars as pl

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


def _value_ratio(player: SquadPlayer) -> float:
    return player.predicted_points / player.price if player.price > 0 else player.predicted_points


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


def _min_remaining_cost(
    pool_by_position: dict[Position, list[SquadPlayer]],
    remaining_quota: dict[Position, int],
    excluded_ids: set[int],
) -> float:
    """A per-position, club-unaware lower bound on the cost to fill every
    still-open slot - deliberately ignores the club cap (a heuristic
    simplification, not an exact feasibility proof, per this module's
    docstring)."""
    total = 0.0
    for position, quota in remaining_quota.items():
        if quota == 0:
            continue
        eligible_prices = sorted(
            candidate.price
            for candidate in pool_by_position[position]
            if candidate.player_id not in excluded_ids
        )
        if len(eligible_prices) < quota:
            return float("inf")
        total += sum(eligible_prices[:quota])
    return total


def pick_squad(predictions: pl.DataFrame, *, budget: float = BUDGET_MILLIONS) -> SquadSelection:
    """Greedy squad construction: fill each position's quota with the best
    predicted-points-per-price candidate that keeps the remaining budget
    sufficient to complete every other still-open slot, and that does not
    breach the per-club cap."""
    candidates = _candidates(predictions)

    pool_by_position: dict[Position, list[SquadPlayer]] = {
        position: [] for position in SQUAD_POSITION_QUOTAS
    }
    for candidate in candidates:
        if candidate.position in pool_by_position:
            pool_by_position[candidate.position].append(candidate)
    for pool in pool_by_position.values():
        pool.sort(key=lambda c: (-_value_ratio(c), -c.predicted_points, c.player_id))

    picked: list[SquadPlayer] = []
    picked_ids: set[int] = set()
    club_counts: dict[int, int] = {}
    remaining_budget = budget
    remaining_quota = dict(SQUAD_POSITION_QUOTAS)

    for position, quota in SQUAD_POSITION_QUOTAS.items():
        for _ in range(quota):
            chosen: SquadPlayer | None = None
            for candidate in pool_by_position[position]:
                if candidate.player_id in picked_ids:
                    continue
                if club_counts.get(candidate.team_id, 0) >= MAX_PLAYERS_PER_CLUB:
                    continue
                if candidate.price > remaining_budget:
                    continue
                hypothetical_quota = dict(remaining_quota)
                hypothetical_quota[position] -= 1
                still_affordable = remaining_budget - candidate.price >= _min_remaining_cost(
                    pool_by_position, hypothetical_quota, picked_ids | {candidate.player_id}
                )
                if still_affordable:
                    chosen = candidate
                    break
            if chosen is None:
                raise ValueError(
                    f"cannot complete squad: no affordable {position} candidate available "
                    "within the remaining budget/club constraints"
                )
            picked.append(chosen)
            picked_ids.add(chosen.player_id)
            club_counts[chosen.team_id] = club_counts.get(chosen.team_id, 0) + 1
            remaining_budget -= chosen.price
            remaining_quota[position] -= 1

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
