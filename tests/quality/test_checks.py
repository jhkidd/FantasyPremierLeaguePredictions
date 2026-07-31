from __future__ import annotations

import polars as pl

from fpl.quality.checks import (
    _defensive_contribution_formula_gate,
    _no_scoreless_appearance_gate,
    _obs_constant_within_season_gate,
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
