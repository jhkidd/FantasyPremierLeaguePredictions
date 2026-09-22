"""Tests for :mod:`fpl.optimiser.squad` (Phase D step 11,
`.github/context/subsystem3-close-and-mvp-site.md`)."""

from __future__ import annotations

import polars as pl
import pytest

from fpl.optimiser.squad import SquadPlayer, SquadSelection, pick_squad, pick_starting_xi


def _row(
    player_id: int,
    team_id: int,
    position: str,
    price: float,
    predicted_points: float | None,
    fixture_id: int = 0,
) -> dict:
    return {
        "player_id": player_id,
        "fixture_id": fixture_id or player_id,
        "team_id": team_id,
        "position": position,
        "price": price,
        "predicted_total_points_fpl": predicted_points,
    }


def _cheap_pool(position: str, team_start: int, count: int, *, price: float = 4.0) -> list[dict]:
    """``count`` interchangeable, cheap filler candidates spread across
    distinct clubs, so a test's real subjects are never blocked by budget
    or the per-club cap when filling the rest of the squad."""
    return [
        _row(1000 + team_start * 100 + i, team_start + i, position, price, 1.0)
        for i in range(count)
    ]


class TestPickSquad:
    def test_picks_a_valid_squad_within_budget_and_quotas(self) -> None:
        rows = [
            *_cheap_pool("GK", 1, 2),
            *_cheap_pool("DEF", 10, 5),
            *_cheap_pool("MID", 20, 5),
            *_cheap_pool("FWD", 30, 3),
        ]
        predictions = pl.DataFrame(rows)

        squad = pick_squad(predictions, budget=100.0)

        assert len(squad.players) == 15
        assert sum(1 for p in squad.players if p.position == "GK") == 2
        assert sum(1 for p in squad.players if p.position == "DEF") == 5
        assert sum(1 for p in squad.players if p.position == "MID") == 5
        assert sum(1 for p in squad.players if p.position == "FWD") == 3
        assert squad.total_price <= 100.0

    def test_prefers_the_higher_value_candidate(self) -> None:
        # Two GK candidates at the same price; only one is needed alongside
        # a cheap filler, so the better points-per-price pick must win.
        rows = [
            _row(1, 1, "GK", 5.0, 10.0),
            _row(2, 2, "GK", 5.0, 1.0),
            _row(900, 90, "GK", 2.0, 1.0),
            *_cheap_pool("DEF", 10, 5),
            *_cheap_pool("MID", 20, 5),
            *_cheap_pool("FWD", 30, 3),
        ]
        predictions = pl.DataFrame(rows)

        squad = pick_squad(predictions, budget=100.0)

        gk_ids = {p.player_id for p in squad.players if p.position == "GK"}
        assert gk_ids == {1, 900}

    def test_respects_max_players_per_club(self) -> None:
        # Four superb DEF candidates from the same club; only 3 may be kept,
        # so a fifth (from a different club) must fill the remaining slot.
        rows = [
            _row(1, 1, "DEF", 5.0, 10.0),
            _row(2, 1, "DEF", 5.0, 9.0),
            _row(3, 1, "DEF", 5.0, 8.0),
            _row(4, 1, "DEF", 5.0, 7.0),
            _row(5, 2, "DEF", 5.0, 1.0),
            *_cheap_pool("DEF", 50, 1),
            *_cheap_pool("GK", 90, 2),
            *_cheap_pool("MID", 20, 5),
            *_cheap_pool("FWD", 30, 3),
        ]
        predictions = pl.DataFrame(rows)

        squad = pick_squad(predictions, budget=100.0)

        club_1_def = [p for p in squad.players if p.team_id == 1]
        assert len(club_1_def) == 3
        assert {p.player_id for p in club_1_def} == {1, 2, 3}
        assert any(p.player_id == 5 for p in squad.players)

    def test_double_gameweek_points_are_summed_before_ranking(self) -> None:
        # Player 1's two fixtures sum to a higher total (8.0) than player
        # 2's single, pricier fixture (7.0 at a worse points-per-price
        # ratio), so player 1 must be preferred and player 2 squeezed out
        # by the two cheaper, better-value fillers.
        rows = [
            _row(1, 1, "FWD", 5.0, 4.0, fixture_id=1),
            _row(1, 1, "FWD", 5.0, 4.0, fixture_id=2),
            _row(2, 2, "FWD", 12.0, 7.0, fixture_id=3),
            _row(41, 41, "FWD", 4.0, 3.0),
            _row(42, 42, "FWD", 4.0, 3.0),
            *_cheap_pool("GK", 1, 2),
            *_cheap_pool("DEF", 10, 5),
            *_cheap_pool("MID", 20, 5),
        ]
        predictions = pl.DataFrame(rows)

        squad = pick_squad(predictions, budget=100.0)

        picked = {p.player_id: p.predicted_points for p in squad.players if p.position == "FWD"}
        assert picked[1] == 8.0
        assert 1 in picked and 2 not in picked

    def test_drops_rows_with_null_predicted_points(self) -> None:
        rows = [
            _row(1, 1, "FWD", 5.0, None),
            *_cheap_pool("GK", 1, 2),
            *_cheap_pool("DEF", 10, 5),
            *_cheap_pool("MID", 20, 5),
            *_cheap_pool("FWD", 30, 3),
        ]
        predictions = pl.DataFrame(rows)

        squad = pick_squad(predictions, budget=100.0)

        assert 1 not in {p.player_id for p in squad.players}

    def test_missing_required_column_raises(self) -> None:
        predictions = pl.DataFrame({"player_id": [1], "team_id": [1]})

        with pytest.raises(ValueError, match="missing required column"):
            pick_squad(predictions)

    def test_raises_when_a_position_cannot_be_filled(self) -> None:
        rows = [
            *_cheap_pool("GK", 1, 1),  # only one GK candidate; two required
            *_cheap_pool("DEF", 10, 5),
            *_cheap_pool("MID", 20, 5),
            *_cheap_pool("FWD", 30, 3),
        ]
        predictions = pl.DataFrame(rows)

        with pytest.raises(ValueError, match="GK"):
            pick_squad(predictions, budget=100.0)


def _player(player_id: int, team_id: int, position: str, points: float) -> SquadPlayer:
    return SquadPlayer(
        player_id=player_id, team_id=team_id, position=position, price=5.0, predicted_points=points
    )


class TestPickStartingXi:
    def test_picks_best_scoring_valid_formation(self) -> None:
        players = (
            _player(1, 1, "GK", 5.0),
            _player(2, 2, "GK", 1.0),  # weaker backup GK
            _player(10, 10, "DEF", 8.0),
            _player(11, 11, "DEF", 7.0),
            _player(12, 12, "DEF", 6.0),
            _player(13, 13, "DEF", 1.0),  # weakest DEF - should be benched
            _player(14, 14, "DEF", 0.5),  # weakest DEF - should be benched
            _player(20, 20, "MID", 9.0),
            _player(21, 21, "MID", 8.5),
            _player(22, 22, "MID", 8.0),
            _player(23, 23, "MID", 7.5),
            _player(24, 24, "MID", 0.1),  # weakest MID - should be benched
            _player(30, 30, "FWD", 6.5),
            _player(31, 31, "FWD", 6.0),
            _player(32, 32, "FWD", 5.5),
        )
        squad = SquadSelection(players=players)

        result = pick_starting_xi(squad)

        assert len(result.starting_xi) == 11
        assert len(result.bench) == 4
        starting_ids = {p.player_id for p in result.starting_xi}
        assert starting_ids == {1, 10, 11, 12, 20, 21, 22, 23, 30, 31, 32}
        bench_ids = {p.player_id for p in result.bench}
        assert bench_ids == {2, 13, 14, 24}

    def test_formation_minimums_are_enforced_even_when_suboptimal(self) -> None:
        # Every MID outscores every DEF, so an unconstrained top-10 pick
        # would field fewer than 3 defenders - the formation rule must win.
        players = (
            _player(1, 1, "GK", 5.0),
            _player(2, 2, "GK", 1.0),
            _player(10, 10, "DEF", 1.0),
            _player(11, 11, "DEF", 0.9),
            _player(12, 12, "DEF", 0.8),
            _player(13, 13, "DEF", 0.7),
            _player(14, 14, "DEF", 0.6),
            _player(20, 20, "MID", 9.0),
            _player(21, 21, "MID", 8.5),
            _player(22, 22, "MID", 8.0),
            _player(23, 23, "MID", 7.5),
            _player(24, 24, "MID", 7.0),
            _player(30, 30, "FWD", 6.5),
            _player(31, 31, "FWD", 6.0),
            _player(32, 32, "FWD", 5.5),
        )
        squad = SquadSelection(players=players)

        result = pick_starting_xi(squad)

        def_count = sum(1 for p in result.starting_xi if p.position == "DEF")
        fwd_count = sum(1 for p in result.starting_xi if p.position == "FWD")
        assert def_count >= 3
        assert fwd_count >= 1

    def test_captain_and_vice_are_the_top_two_scorers_in_the_xi(self) -> None:
        players = (
            _player(1, 1, "GK", 5.0),
            _player(2, 2, "GK", 1.0),
            _player(10, 10, "DEF", 2.0),
            _player(11, 11, "DEF", 2.0),
            _player(12, 12, "DEF", 2.0),
            _player(13, 13, "DEF", 0.5),
            _player(14, 14, "DEF", 0.4),
            _player(20, 20, "MID", 20.0),  # best - captain
            _player(21, 21, "MID", 15.0),  # second best - vice
            _player(22, 22, "MID", 3.0),
            _player(23, 23, "MID", 2.5),
            _player(24, 24, "MID", 0.1),
            _player(30, 30, "FWD", 6.5),
            _player(31, 31, "FWD", 6.0),
            _player(32, 32, "FWD", 5.5),
        )
        squad = SquadSelection(players=players)

        result = pick_starting_xi(squad)

        assert result.captain.player_id == 20
        assert result.vice_captain.player_id == 21

    def test_bench_lists_the_goalkeeper_first_then_by_descending_points(self) -> None:
        players = (
            _player(1, 1, "GK", 5.0),
            _player(2, 2, "GK", 1.0),
            _player(10, 10, "DEF", 8.0),
            _player(11, 11, "DEF", 7.0),
            _player(12, 12, "DEF", 6.0),
            _player(13, 13, "DEF", 1.0),
            _player(14, 14, "DEF", 2.0),
            _player(20, 20, "MID", 9.0),
            _player(21, 21, "MID", 8.5),
            _player(22, 22, "MID", 8.0),
            _player(23, 23, "MID", 7.5),
            _player(24, 24, "MID", 0.1),
            _player(30, 30, "FWD", 6.5),
            _player(31, 31, "FWD", 6.0),
            _player(32, 32, "FWD", 5.5),
        )
        squad = SquadSelection(players=players)

        result = pick_starting_xi(squad)

        assert result.bench[0].player_id == 2
        rest = result.bench[1:]
        assert [p.predicted_points for p in rest] == sorted(
            (p.predicted_points for p in rest), reverse=True
        )

    def test_wrong_goalkeeper_count_raises(self) -> None:
        players = (
            _player(1, 1, "GK", 5.0),
            *[_player(100 + i, 100 + i, "DEF", 1.0) for i in range(5)],
            *[_player(200 + i, 200 + i, "MID", 1.0) for i in range(5)],
            *[_player(300 + i, 300 + i, "FWD", 1.0) for i in range(4)],
        )
        squad = SquadSelection(players=players)

        with pytest.raises(ValueError, match="goalkeeper"):
            pick_starting_xi(squad)
