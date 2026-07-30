from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fpl import exit_codes
from fpl.cli import app
from fpl.sources import fpl_api
from fpl.sources.errors import BlockedError, SchemaError, SourceError

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
        ["ingest", "understat"],
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
    result = runner.invoke(app, ["ingest", "understat", "--season", "2016-17"])
    assert result.exit_code == exit_codes.NOT_IMPLEMENTED


class TestIngestExitCodes:
    """Workflows branch on these, so each failure mode gets its own code:
    a block needs a human, a schema change needs a code change, and everything
    else is worth retrying."""

    def _invoke(self, monkeypatch: pytest.MonkeyPatch, error: Exception):
        def boom(*_args: object, **_kwargs: object) -> None:
            raise error

        monkeypatch.setattr("fpl.cli.ingest_fpl", boom)
        return runner.invoke(app, ["ingest", "fpl"])

    def test_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._invoke(
            monkeypatch, BlockedError("403", url="https://x", headers={"cf-ray": "abc"})
        )
        assert result.exit_code == exit_codes.BLOCKED
        assert "Not retrying" in result.output

    def test_schema_change(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._invoke(monkeypatch, SchemaError("elements missing", url="https://x"))
        assert result.exit_code == exit_codes.SCHEMA_CHANGED

    def test_other_source_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._invoke(monkeypatch, SourceError("timed out", url="https://x"))
        assert result.exit_code == exit_codes.FAILURE

    def test_bad_argument_combination_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, ValueError("event-live requires --event"))
        assert result.exit_code == exit_codes.USAGE


class TestIngestSuccess:
    @respx.mock
    def test_reports_what_it_wrote(self, tmp_path: Path) -> None:
        fixtures = Path(__file__).resolve().parent / "fixtures" / "fpl"
        respx.get(f"{fpl_api.BASE_URL}/bootstrap-static/").mock(
            return_value=httpx.Response(
                200, json=json.loads((fixtures / "bootstrap_static.json").read_text("utf-8"))
            )
        )
        respx.get(f"{fpl_api.BASE_URL}/fixtures/").mock(
            return_value=httpx.Response(
                200, json=json.loads((fixtures / "fixtures.json").read_text("utf-8"))
            )
        )
        argv = ["--data-root", str(tmp_path), "ingest", "fpl", "--season", "2026-27"]
        result = runner.invoke(app, argv)
        assert result.exit_code == exit_codes.SUCCESS, result.output
        assert "2 endpoint(s) pulled, 2 written" in result.output

        again = runner.invoke(app, argv)
        assert "0 written, 2 unchanged" in again.output


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
