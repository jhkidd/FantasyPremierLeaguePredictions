from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fpl import exit_codes
from fpl.cli import app
from fpl.config import Season
from fpl.sources import fpl_api
from fpl.sources.errors import BlockedError, SchemaError, SourceError
from fpl.storage.raw_io import RawArtifact, write_raw

runner = CliRunner()

# The capture window is "deadline passed, gameweek not finished". These are
# fixed rather than relative to now so the tests mean the same thing whenever
# they run — including after the real 2026/27 season has started.
OPEN_DEADLINE = "2026-07-01T00:00:00Z"
FUTURE_DEADLINE = "2099-01-01T00:00:00Z"


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


class TestCaptureOwnershipCommand:
    """Runs every 30 minutes and does nothing almost every time, so the
    do-nothing paths matter more than the working one."""

    def _bootstrap(self, root: Path, *, deadline: str, finished: bool = False) -> None:
        artifact = RawArtifact(
            source="fpl",
            endpoint="bootstrap_static",
            season=Season(2026),
            url="https://x",
            http_status=200,
            body=json.dumps(
                {"events": [{"id": 1, "deadline_time": deadline, "finished": finished}]}
            ).encode(),
            fetched_at=datetime(2026, 8, 1, tzinfo=UTC),
            connector_version="1",
        )
        write_raw(artifact, data_root=root)

    def test_requires_a_stored_bootstrap(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["--data-root", str(tmp_path), "capture-ownership"])
        assert result.exit_code == exit_codes.FAILURE
        assert "ingest fpl" in result.output

    def test_no_open_gameweek_exits_successfully(self, tmp_path: Path) -> None:
        """'Nothing to capture' is the normal state, not a failure."""
        self._bootstrap(tmp_path, deadline=FUTURE_DEADLINE)
        result = runner.invoke(
            app, ["--data-root", str(tmp_path), "capture-ownership", "--league", "999"]
        )
        assert result.exit_code == exit_codes.SUCCESS
        assert "nothing_to_do" in result.output

    def test_dry_run_reports_the_plan_without_fetching(self, tmp_path: Path) -> None:
        self._bootstrap(tmp_path, deadline=OPEN_DEADLINE)
        with respx.mock:
            result = runner.invoke(
                app,
                [
                    "--data-root",
                    str(tmp_path),
                    "capture-ownership",
                    "--league",
                    "999",
                    "--dry-run",
                ],
            )
        assert result.exit_code == exit_codes.SUCCESS
        assert "would capture cohort=mini" in result.output

    def test_elite_cohort_is_not_attempted_in_gameweek_one(self, tmp_path: Path) -> None:
        """The overall league has no ranking until a gameweek has been scored,
        so an open GW1 yields work for the mini cohort but not the elite one."""
        self._bootstrap(tmp_path, deadline=OPEN_DEADLINE)
        elite = runner.invoke(
            app,
            ["--data-root", str(tmp_path), "capture-ownership", "--cohort", "elite", "--dry-run"],
        )
        assert elite.exit_code == exit_codes.SUCCESS
        assert "nothing_to_do" in elite.output

        mini = runner.invoke(
            app,
            [
                "--data-root",
                str(tmp_path),
                "capture-ownership",
                "--cohort",
                "mini",
                "--league",
                "999",
                "--dry-run",
            ],
        )
        assert "would capture cohort=mini" in mini.output

    def test_missing_mini_league_warns_but_does_not_stop_the_run(self, tmp_path: Path) -> None:
        self._bootstrap(tmp_path, deadline=OPEN_DEADLINE)
        result = runner.invoke(
            app, ["--data-root", str(tmp_path), "capture-ownership", "--dry-run"]
        )
        assert result.exit_code == exit_codes.SUCCESS
        assert "No mini-league configured" in result.output

    def test_asking_for_mini_without_a_league_is_a_usage_error(self, tmp_path: Path) -> None:
        self._bootstrap(tmp_path, deadline=OPEN_DEADLINE)
        result = runner.invoke(
            app, ["--data-root", str(tmp_path), "capture-ownership", "--cohort", "mini"]
        )
        assert result.exit_code == exit_codes.USAGE

    def test_unknown_cohort_is_rejected(self, tmp_path: Path) -> None:
        self._bootstrap(tmp_path, deadline=OPEN_DEADLINE)
        result = runner.invoke(
            app, ["--data-root", str(tmp_path), "capture-ownership", "--cohort", "nonsense"]
        )
        assert result.exit_code == exit_codes.USAGE

    @respx.mock
    def test_captures_a_mini_league_end_to_end(self, tmp_path: Path) -> None:
        self._bootstrap(tmp_path, deadline=OPEN_DEADLINE)
        respx.get(f"{fpl_api.BASE_URL}/leagues-classic/999/standings/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "standings": {
                        "results": [{"entry": 7}, {"entry": 8}],
                        "has_next": False,
                        "page": 1,
                    }
                },
            )
        )
        for entry_id in (7, 8):
            respx.get(f"{fpl_api.BASE_URL}/entry/{entry_id}/event/1/picks/").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "automatic_subs": [],
                        "picks": [{"element": 1, "position": 1, "multiplier": 1}],
                    },
                )
            )
        result = runner.invoke(
            app,
            [
                "--data-root",
                str(tmp_path),
                "capture-ownership",
                "--cohort",
                "mini",
                "--league",
                "999",
            ],
        )
        assert result.exit_code == exit_codes.SUCCESS, result.output
        assert "cohort=mini event=1 entries=2 chunks_written=1" in result.output

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
