from __future__ import annotations

import polars as pl

from fpl.quality.checks import (
    _defensive_contribution_formula_gate,
    _elo_within_validity_window_gate,
    _fixture_has_two_teams_gate,
    _no_scoreless_appearance_gate,
    _obs_constant_within_season_gate,
    _team_is_not_its_own_opponent_gate,
)
from fpl.quality.gates import run_gates


class TestNoScorelessAppearanceGate:
    def test_passes_when_minutes_and_points_agree(self) -> None:
        frame = pl.DataFrame({"minutes": [0, 90], "total_points_fpl": [0, 6]})
        assert run_gates(frame, [_no_scoreless_appearance_gate()]) == []

    def test_fails_when_a_row_scores_with_no_minutes(self) -> None:
        frame = pl.DataFrame({"minutes": [0], "total_points_fpl": [2]})
        violations = run_gates(frame, [_no_scoreless_appearance_gate()])
        assert len(violations) == 1
        assert violations[0].rows == 1
        assert violations[0].severity == "block"


class TestDefensiveContributionFormulaGate:
    def test_passes_when_formula_holds_per_position(self) -> None:
        frame = pl.DataFrame(
            {
                "position": ["DEF", "MID"],
                "cbi": [8, 5],
                "tackles": [2, 3],
                "recoveries": [1, 4],
                "defensive_contribution": [10, 12],  # DEF: cbi+tackles, MID: +recoveries
                "obs_defensive": [True, True],
            }
        )
        assert run_gates(frame, [_defensive_contribution_formula_gate()]) == []

    def test_fails_when_defender_total_disagrees(self) -> None:
        frame = pl.DataFrame(
            {
                "position": ["DEF"],
                "cbi": [8],
                "tackles": [2],
                "recoveries": [1],
                "defensive_contribution": [99],
                "obs_defensive": [True],
            }
        )
        violations = run_gates(frame, [_defensive_contribution_formula_gate()])
        assert len(violations) == 1
        assert violations[0].severity == "warn"

    def test_ignores_goalkeepers(self) -> None:
        frame = pl.DataFrame(
            {
                "position": ["GK"],
                "cbi": [1],
                "tackles": [0],
                "recoveries": [0],
                "defensive_contribution": [999],
                "obs_defensive": [True],
            }
        )
        assert run_gates(frame, [_defensive_contribution_formula_gate()]) == []


class TestObsConstantWithinSeasonGate:
    def test_passes_when_flags_are_uniform(self) -> None:
        frame = pl.DataFrame({"obs_defensive": [True, True, True]})
        assert run_gates(frame, [_obs_constant_within_season_gate()]) == []

    def test_fails_when_flag_varies_within_a_season(self) -> None:
        frame = pl.DataFrame({"obs_defensive": [True, False]})
        violations = run_gates(frame, [_obs_constant_within_season_gate()])
        assert len(violations) == 1
        assert violations[0].severity == "block"


class TestFixtureHasTwoTeamsGate:
    """Guards the invariant the ``team_id`` repair rests on (plan §0.3).

    Derivation only works because a fixture's two teams are exactly its two
    distinct ``opponent_team_id`` values. If that ever stops holding, every
    ``team_id`` downstream is quietly wrong rather than absent, so the
    invariant is asserted at ``fpl check`` time rather than trusted.
    """

    def test_passes_when_every_fixture_has_exactly_two(self) -> None:
        frame = pl.DataFrame(
            {
                "fixture_id": [1, 1, 1, 2, 2],
                "opponent_team_id": [19, 19, 17, 3, 8],
            }
        )
        assert run_gates(frame, [_fixture_has_two_teams_gate()]) == []

    def test_fails_when_a_fixture_names_three_opponents(self) -> None:
        frame = pl.DataFrame(
            {
                "fixture_id": [1, 1, 1],
                "opponent_team_id": [19, 17, 5],
            }
        )
        violations = run_gates(frame, [_fixture_has_two_teams_gate()])
        assert len(violations) == 1
        assert violations[0].severity == "block"
        assert "1" in violations[0].detail

    def test_fails_when_a_fixture_is_one_sided(self) -> None:
        """A one-sided fixture is exactly the case the derivation cannot
        resolve, so it must surface here rather than as silent nulls."""
        frame = pl.DataFrame({"fixture_id": [1, 1], "opponent_team_id": [19, 19]})
        violations = run_gates(frame, [_fixture_has_two_teams_gate()])
        assert len(violations) == 1
        assert violations[0].severity == "block"

    def test_null_opponents_do_not_count_toward_the_two(self) -> None:
        frame = pl.DataFrame(
            {
                "fixture_id": [1, 1, 1],
                "opponent_team_id": [19, 17, None],
            }
        )
        assert run_gates(frame, [_fixture_has_two_teams_gate()]) == []

    def test_reports_every_offending_fixture_not_just_the_first(self) -> None:
        frame = pl.DataFrame(
            {
                "fixture_id": [1, 1, 1, 2, 2, 2],
                "opponent_team_id": [19, 17, 5, 3, 8, 11],
            }
        )
        violations = run_gates(frame, [_fixture_has_two_teams_gate()])
        assert violations[0].rows == 2

    def test_missing_columns_are_reported_rather_than_crashing(self) -> None:
        violations = run_gates(pl.DataFrame({"fixture_id": [1]}), [_fixture_has_two_teams_gate()])
        assert len(violations) == 1
        assert "opponent_team_id" in violations[0].detail


class TestTeamIsNotItsOwnOpponentGate:
    def test_passes_when_the_two_differ(self) -> None:
        frame = pl.DataFrame({"team_id": [17, 19], "opponent_team_id": [19, 17]})
        assert run_gates(frame, [_team_is_not_its_own_opponent_gate()]) == []

    def test_fails_when_a_team_faces_itself(self) -> None:
        frame = pl.DataFrame({"team_id": [17], "opponent_team_id": [17]})
        violations = run_gates(frame, [_team_is_not_its_own_opponent_gate()])
        assert len(violations) == 1
        assert violations[0].rows == 1
        assert violations[0].severity == "block"

    def test_null_team_id_is_left_to_the_not_null_gate(self) -> None:
        """Overlapping gates would report the same defect twice and make a
        failure log harder to read, so nulls are one gate's job only."""
        frame = pl.DataFrame({"team_id": [None], "opponent_team_id": [17]})
        assert run_gates(frame, [_team_is_not_its_own_opponent_gate()]) == []


class TestEloWithinValidityWindowGate:
    """Club Elo publishes the span each rating covers, making the source
    self-checking (plan §0.5). The date-stamping bug was invisible to every
    null-based check because the column was fully populated and merely wrong;
    this gate is the one that would have caught it immediately.
    """

    def test_passes_when_stamped_inside_the_window(self) -> None:
        frame = pl.DataFrame(
            {
                "as_of_date": ["2025-06-15"],
                "valid_from": ["2025-05-31"],
                "valid_to": ["2025-08-21"],
            }
        )
        assert run_gates(frame, [_elo_within_validity_window_gate()]) == []

    def test_passes_on_the_window_boundaries(self) -> None:
        frame = pl.DataFrame(
            {
                "as_of_date": ["2025-05-31", "2025-08-21"],
                "valid_from": ["2025-05-31", "2025-05-31"],
                "valid_to": ["2025-08-21", "2025-08-21"],
            }
        )
        assert run_gates(frame, [_elo_within_validity_window_gate()]) == []

    def test_fails_when_stamped_after_the_window(self) -> None:
        """The exact signature of the original bug: fetched in August, the
        ratings were May's, so every row landed months past its own window."""
        frame = pl.DataFrame(
            {
                "as_of_date": ["2026-08-03"],
                "valid_from": ["2025-05-31"],
                "valid_to": ["2025-08-21"],
            }
        )
        violations = run_gates(frame, [_elo_within_validity_window_gate()])
        assert len(violations) == 1
        assert violations[0].rows == 1
        assert violations[0].severity == "block"

    def test_fails_when_stamped_before_the_window(self) -> None:
        frame = pl.DataFrame(
            {
                "as_of_date": ["2025-01-01"],
                "valid_from": ["2025-05-31"],
                "valid_to": ["2025-08-21"],
            }
        )
        assert len(run_gates(frame, [_elo_within_validity_window_gate()])) == 1

    def test_unparseable_window_bounds_are_skipped_not_flagged(self) -> None:
        """An open-ended or malformed bound says nothing about whether our
        stamp is wrong, and flagging it would bury the real failures."""
        frame = pl.DataFrame(
            {
                "as_of_date": ["2025-06-15"],
                "valid_from": [None],
                "valid_to": ["not-a-date"],
            }
        )
        assert run_gates(frame, [_elo_within_validity_window_gate()]) == []

    def test_missing_columns_are_reported_rather_than_crashing(self) -> None:
        violations = run_gates(
            pl.DataFrame({"as_of_date": ["2025-06-15"]}), [_elo_within_validity_window_gate()]
        )
        assert len(violations) == 1
        assert "valid_from" in violations[0].detail
