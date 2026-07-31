from __future__ import annotations

import polars as pl

from fpl.quality.gates import (
    enum_values,
    has_blocking_violations,
    in_range,
    non_negative,
    not_null,
    referential,
    run_gates,
    unique_key,
)


class TestUniqueKey:
    def test_passes_on_unique_frame(self):
        frame = pl.DataFrame({"season": ["2025-26", "2025-26"], "player_id": [1, 2]})
        assert run_gates(frame, [unique_key(["season", "player_id"])]) == []

    def test_fails_on_duplicate_frame(self):
        frame = pl.DataFrame({"season": ["2025-26", "2025-26"], "player_id": [1, 1]})
        violations = run_gates(frame, [unique_key(["season", "player_id"])])
        assert len(violations) == 1
        assert violations[0].rows == 2
        assert violations[0].sample


class TestNotNull:
    def test_passes_when_no_nulls(self):
        frame = pl.DataFrame({"x": [1, 2]})
        assert run_gates(frame, [not_null("x")]) == []

    def test_fails_when_null_present(self):
        frame = pl.DataFrame({"x": [1, None]})
        violations = run_gates(frame, [not_null("x")])
        assert violations[0].rows == 1


class TestInRange:
    def test_passes_within_bounds(self):
        frame = pl.DataFrame({"minutes": [0, 60, 90]})
        assert run_gates(frame, [in_range("minutes", minimum=0, maximum=120)]) == []

    def test_fails_outside_bounds(self):
        frame = pl.DataFrame({"minutes": [0, 130]})
        violations = run_gates(frame, [in_range("minutes", minimum=0, maximum=120)])
        assert violations[0].rows == 1

    def test_nulls_are_ignored(self):
        frame = pl.DataFrame({"minutes": [None, 200]}, schema={"minutes": pl.Int64})
        violations = run_gates(frame, [in_range("minutes", minimum=0, maximum=120)])
        assert violations[0].rows == 1


class TestNonNegative:
    def test_fails_on_negative_value(self):
        frame = pl.DataFrame({"goals": [-1, 2]})
        violations = run_gates(frame, [non_negative("goals")])
        assert violations[0].rows == 1


class TestEnumValues:
    def test_fails_on_unexpected_value(self):
        frame = pl.DataFrame({"position": ["GK", "GKP", "DEF"]})
        violations = run_gates(frame, [enum_values("position", ["GK", "DEF", "MID", "FWD"])])
        assert violations[0].rows == 1
        assert "GKP" in violations[0].detail


class TestReferential:
    def test_fails_when_value_not_in_reference(self):
        frame = pl.DataFrame({"team_id": [1, 2, 99]})
        reference = pl.DataFrame({"team_id": [1, 2, 3]})
        violations = run_gates(frame, [referential("team_id", reference, "team_id")])
        assert violations[0].rows == 1


class TestRunGatesAggregates:
    def test_reports_every_gate_not_just_the_first(self):
        frame = pl.DataFrame({"x": [1, None], "y": [-1, -2]})
        violations = run_gates(frame, [not_null("x"), non_negative("y")])
        assert len(violations) == 2


class TestHasBlockingViolations:
    def test_true_when_a_block_violation_present(self):
        frame = pl.DataFrame({"x": [None]})
        violations = run_gates(frame, [not_null("x", severity="block")])
        assert has_blocking_violations(violations) is True

    def test_false_when_only_warnings_present(self):
        frame = pl.DataFrame({"x": [None]})
        violations = run_gates(frame, [not_null("x", severity="warn")])
        assert has_blocking_violations(violations) is False

    def test_false_when_no_violations(self):
        assert has_blocking_violations([]) is False
