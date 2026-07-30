from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fpl import exit_codes
from fpl.cli import app

runner = CliRunner()


def test_help_lists_the_intended_surface() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == exit_codes.SUCCESS
    for command in ("ingest", "stage", "facts", "crosswalk", "check", "features", "backfill"):
        assert command in result.output


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == exit_codes.SUCCESS
    assert result.output.strip()


@pytest.mark.parametrize(
    "argv",
    [
        ["ingest", "fpl"],
        ["stage", "fpl"],
        ["facts"],
        ["crosswalk", "refresh"],
        ["crosswalk", "validate"],
        ["check"],
        ["features", "--as-of", "2026-08-14T11:30:00Z"],
        ["backfill"],
    ],
)
def test_unbuilt_commands_exit_distinctly_and_name_their_phase(argv: list[str]) -> None:
    """Visible and honestly unfinished beats absent: the exit code is distinct
    from both success and a usage error, and the message says when it arrives."""
    result = runner.invoke(app, argv)
    assert result.exit_code == exit_codes.NOT_IMPLEMENTED
    assert "phase" in result.output


@pytest.mark.parametrize("season", ["2026-28", "not-a-season", "2026", "2026/27"])
def test_malformed_season_is_rejected_before_anything_else(season: str) -> None:
    result = runner.invoke(app, ["ingest", "fpl", "--season", season])
    assert result.exit_code == exit_codes.USAGE


def test_valid_season_passes_validation() -> None:
    result = runner.invoke(app, ["ingest", "fpl", "--season", "2016-17"])
    assert result.exit_code == exit_codes.NOT_IMPLEMENTED


def test_no_arguments_shows_help() -> None:
    assert "Usage:" in runner.invoke(app, []).output


def test_exit_codes_are_mutually_distinct() -> None:
    """Workflows branch on these. Two meanings sharing a code would make a
    blocked runner indistinguishable from an ordinary failure."""
    codes = [
        exit_codes.SUCCESS,
        exit_codes.FAILURE,
        exit_codes.USAGE,
        exit_codes.NOT_IMPLEMENTED,
        exit_codes.BLOCKED,
        exit_codes.SCHEMA_CHANGED,
        exit_codes.QUALITY_GATE_FAILED,
    ]
    assert len(set(codes)) == len(codes)
