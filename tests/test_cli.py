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
    for command in (
        "ingest",
        "stage",
        "facts",
        "crosswalk",
        "check",
        "features",
        "dataset",
        "backfill",
    ):
        assert command in result.output


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == exit_codes.SUCCESS
    assert result.output.strip()


@pytest.mark.parametrize(
    "argv",
    [],
)
def test_unbuilt_commands_exit_distinctly_and_name_their_phase(argv: list[str]) -> None:
    """Visible and honestly unfinished beats absent: the exit code is distinct
    from both success and a usage error, and the message says when it arrives."""
    result = runner.invoke(app, argv)
    assert result.exit_code == exit_codes.NOT_IMPLEMENTED
    assert "phase" in result.output


def test_features_with_no_staged_data_fails_with_detail(isolated_data_root: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--data-root",
            str(isolated_data_root),
            "features",
            "--season",
            "2025-26",
            "--as-of",
            "2026-08-14T11:30:00Z",
        ],
    )
    assert result.exit_code == exit_codes.FAILURE
    assert "features: skipped" in result.output


def test_stage_fpl_with_no_raw_data_reports_nothing_captured(isolated_data_root: Path) -> None:
    result = runner.invoke(app, ["--data-root", str(isolated_data_root), "stage", "fpl"])
    assert result.exit_code == exit_codes.SUCCESS
    assert "no bootstrap-static capture on disk" in result.output


def test_stage_vaastav_with_no_raw_data_reports_nothing_captured(
    isolated_data_root: Path,
) -> None:
    result = runner.invoke(
        app,
        ["--data-root", str(isolated_data_root), "stage", "vaastav", "--season", "2025-26"],
    )
    assert result.exit_code == exit_codes.SUCCESS
    assert "no vaastav merged_gw capture on disk" in result.output


@pytest.mark.parametrize(
    "source, expected_detail",
    [
        ("clubelo", "no clubelo ratings capture on disk"),
        ("footballdata", "no footballdata matches_and_odds capture on disk"),
        ("openfootball", "no openfootball capture on disk"),
    ],
)
def test_stage_tier2_sources_with_no_raw_data_reports_nothing_captured(
    isolated_data_root: Path, source: str, expected_detail: str
) -> None:
    result = runner.invoke(
        app,
        ["--data-root", str(isolated_data_root), "stage", source, "--season", "2025-26"],
    )
    assert result.exit_code == exit_codes.SUCCESS
    assert expected_detail in result.output


def test_facts_with_no_staged_data_reports_nothing_to_assemble(
    isolated_data_root: Path,
) -> None:
    result = runner.invoke(
        app,
        ["--data-root", str(isolated_data_root), "facts", "--season", "2025-26"],
    )
    assert result.exit_code == exit_codes.SUCCESS
    assert "skipped" in result.output


def test_check_with_no_staged_data_is_clean(isolated_data_root: Path) -> None:
    result = runner.invoke(app, ["--data-root", str(isolated_data_root), "check"])
    assert result.exit_code == exit_codes.SUCCESS
    assert "clean" in result.output


def test_crosswalk_refresh_with_no_ingested_data_writes_empty_crosswalks(
    isolated_data_root: Path,
) -> None:
    result = runner.invoke(app, ["--data-root", str(isolated_data_root), "crosswalk", "refresh"])
    assert result.exit_code == exit_codes.SUCCESS
    assert (isolated_data_root / "crosswalk" / "players_fpl.csv").is_file()
    assert (isolated_data_root / "crosswalk" / "teams.csv").is_file()
    assert (isolated_data_root / "crosswalk" / "team_external_ids.csv").is_file()


def test_crosswalk_validate_with_no_ingested_data_fails_the_gate(
    isolated_data_root: Path,
) -> None:
    result = runner.invoke(app, ["--data-root", str(isolated_data_root), "crosswalk", "validate"])
    assert result.exit_code == exit_codes.QUALITY_GATE_FAILED


def test_backfill_skip_fetch_with_no_raw_data_fails_loudly_on_the_first_season(
    isolated_data_root: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "--data-root",
            str(isolated_data_root),
            "backfill",
            "--from",
            "2016-17",
            "--to",
            "2016-17",
            "--skip-fetch",
        ],
    )
    assert result.exit_code == exit_codes.FAILURE


@pytest.mark.parametrize("layer", ["staged", "facts", "both"])
def test_check_layer_option_is_clean_with_no_data(isolated_data_root: Path, layer: str) -> None:
    result = runner.invoke(app, ["--data-root", str(isolated_data_root), "check", "--layer", layer])
    assert result.exit_code == exit_codes.SUCCESS
    assert "clean" in result.output


def test_check_rejects_unknown_layer(isolated_data_root: Path) -> None:
    result = runner.invoke(
        app, ["--data-root", str(isolated_data_root), "check", "--layer", "bronze"]
    )
    assert result.exit_code == exit_codes.USAGE


@pytest.mark.parametrize("season", ["2026-28", "not-a-season", "2026", "2026/27"])
def test_malformed_season_is_rejected_before_anything_else(season: str) -> None:
    result = runner.invoke(app, ["ingest", "fpl", "--season", season])
    assert result.exit_code == exit_codes.USAGE


def test_valid_season_passes_validation() -> None:
    result = runner.invoke(app, ["ingest", "footballdataorg", "--season", "2016-17"])
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

    @pytest.fixture(autouse=True)
    def _no_real_network(self):
        """Every test in this class reaches for the live bootstrap, so the
        router is always on: an unmocked request fails loudly rather than
        hitting the real API."""
        with respx.mock:
            yield

    def _bootstrap(self, *, deadline: str, finished: bool = False) -> None:
        """Serve bootstrap live rather than from disk.

        Capture reads the API directly so it sees current `finished` flags
        without committing a snapshot on every half-hourly tick.
        """
        respx.get(f"{fpl_api.BASE_URL}/bootstrap-static/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "events": [{"id": 1, "deadline_time": deadline, "finished": finished}],
                    "elements": [{"id": 1}],
                    "teams": [{"id": 1}],
                },
            )
        )

    def _stored_bootstrap(self, root: Path, *, deadline: str) -> None:
        artifact = RawArtifact(
            source="fpl",
            endpoint="bootstrap_static",
            season=Season(2026),
            url="https://x",
            http_status=200,
            body=json.dumps(
                {"events": [{"id": 1, "deadline_time": deadline, "finished": False}]}
            ).encode(),
            fetched_at=datetime(2026, 8, 1, tzinfo=UTC),
            connector_version="1",
        )
        write_raw(artifact, data_root=root)

    def test_requires_a_bootstrap_from_somewhere(self, tmp_path: Path) -> None:
        """Live fetch unmocked and nothing on disk: there is no way to know
        which gameweek is open, so refuse rather than guess."""
        result = runner.invoke(app, ["--data-root", str(tmp_path), "capture-ownership"])
        assert result.exit_code == exit_codes.FAILURE
        assert "ingest fpl" in result.output

    def test_falls_back_to_the_stored_copy_when_the_api_is_down(self, tmp_path: Path) -> None:
        """Deadlines do not move, so a day-old snapshot still resolves the
        gameweek correctly. An unreachable API must not skip a capture."""
        self._stored_bootstrap(tmp_path, deadline=OPEN_DEADLINE)
        result = runner.invoke(
            app,
            ["--data-root", str(tmp_path), "capture-ownership", "--league", "999", "--dry-run"],
        )
        assert result.exit_code == exit_codes.SUCCESS
        assert "would capture cohort=mini" in result.output

    def test_no_open_gameweek_exits_successfully(self, tmp_path: Path) -> None:
        """'Nothing to capture' is the normal state, not a failure."""
        self._bootstrap(deadline=FUTURE_DEADLINE)
        result = runner.invoke(
            app, ["--data-root", str(tmp_path), "capture-ownership", "--league", "999"]
        )
        assert result.exit_code == exit_codes.SUCCESS
        assert "nothing_to_do" in result.output

    def test_resolving_the_gameweek_writes_nothing(self, tmp_path: Path) -> None:
        """The job ticks 48 times a day. If each tick persisted bootstrap it
        would commit ~5 MB daily and duplicate the daily snapshot."""
        self._bootstrap(deadline=FUTURE_DEADLINE)
        runner.invoke(app, ["--data-root", str(tmp_path), "capture-ownership", "--league", "999"])
        assert list(tmp_path.rglob("*.json.gz")) == []

    def test_dry_run_reports_the_plan_without_fetching(self, tmp_path: Path) -> None:
        self._bootstrap(deadline=OPEN_DEADLINE)
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
        self._bootstrap(deadline=OPEN_DEADLINE)
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
        self._bootstrap(deadline=OPEN_DEADLINE)
        result = runner.invoke(
            app, ["--data-root", str(tmp_path), "capture-ownership", "--dry-run"]
        )
        assert result.exit_code == exit_codes.SUCCESS
        assert "No mini-league configured" in result.output

    def test_asking_for_mini_without_a_league_is_a_usage_error(self, tmp_path: Path) -> None:
        self._bootstrap(deadline=OPEN_DEADLINE)
        result = runner.invoke(
            app, ["--data-root", str(tmp_path), "capture-ownership", "--cohort", "mini"]
        )
        assert result.exit_code == exit_codes.USAGE

    def test_unknown_cohort_is_rejected(self, tmp_path: Path) -> None:
        self._bootstrap(deadline=OPEN_DEADLINE)
        result = runner.invoke(
            app, ["--data-root", str(tmp_path), "capture-ownership", "--cohort", "nonsense"]
        )
        assert result.exit_code == exit_codes.USAGE

    def test_captures_a_mini_league_end_to_end(self, tmp_path: Path) -> None:
        self._bootstrap(deadline=OPEN_DEADLINE)
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

    def _mock_entry(self, entry_id: int, classic: list[dict]) -> None:
        respx.get(f"{fpl_api.BASE_URL}/entry/{entry_id}/").mock(
            return_value=httpx.Response(
                200, json={"id": entry_id, "leagues": {"classic": classic, "h2h": []}}
            )
        )

    def test_discovers_the_mini_league_from_our_own_team(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nobody has to notice the league was created and paste its ID in."""
        monkeypatch.setenv("FPL_ENTRY_ID", "2282251")
        self._bootstrap(deadline=OPEN_DEADLINE)
        self._mock_entry(
            2282251,
            [
                {"id": 314, "name": "Overall", "league_type": "s"},
                {"id": 555001, "name": "The Office", "league_type": "x"},
            ],
        )
        result = runner.invoke(
            app, ["--data-root", str(tmp_path), "capture-ownership", "--dry-run"]
        )
        assert result.exit_code == exit_codes.SUCCESS, result.output
        assert "Discovered mini-league The Office (555001)" in result.output
        assert "would capture cohort=mini league=555001" in result.output

    def test_an_explicit_league_is_not_overridden_by_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FPL_ENTRY_ID", "2282251")
        self._bootstrap(deadline=OPEN_DEADLINE)
        result = runner.invoke(
            app,
            ["--data-root", str(tmp_path), "capture-ownership", "--league", "42", "--dry-run"],
        )
        assert "would capture cohort=mini league=42" in result.output

    def test_several_private_leagues_captures_none_and_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Capturing the wrong opponents is worse than capturing none and
        being told which candidates exist."""
        monkeypatch.setenv("FPL_ENTRY_ID", "2282251")
        self._bootstrap(deadline=OPEN_DEADLINE)
        self._mock_entry(
            2282251,
            [
                {"id": 111, "name": "Office", "league_type": "x"},
                {"id": 222, "name": "Family", "league_type": "x"},
            ],
        )
        result = runner.invoke(
            app, ["--data-root", str(tmp_path), "capture-ownership", "--dry-run"]
        )
        assert result.exit_code == exit_codes.SUCCESS
        assert "Office (111)" in result.output and "Family (222)" in result.output
        assert "cohort=mini" not in result.output

    def test_our_own_squad_is_captured_from_gameweek_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FPL_ENTRY_ID", "2282251")
        self._bootstrap(deadline=OPEN_DEADLINE)
        self._mock_entry(2282251, [])
        respx.get(f"{fpl_api.BASE_URL}/entry/2282251/event/1/picks/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "automatic_subs": [],
                    "picks": [{"element": 1, "position": 1, "multiplier": 1}],
                },
            )
        )
        result = runner.invoke(
            app, ["--data-root", str(tmp_path), "capture-ownership", "--cohort", "self"]
        )
        assert result.exit_code == exit_codes.SUCCESS, result.output
        assert "cohort=self event=1 entries=1 chunks_written=1" in result.output

    def test_asking_for_self_without_a_team_is_a_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FPL_ENTRY_ID", raising=False)
        self._bootstrap(deadline=OPEN_DEADLINE)
        result = runner.invoke(
            app, ["--data-root", str(tmp_path), "capture-ownership", "--cohort", "self"]
        )
        assert result.exit_code == exit_codes.USAGE

    def test_discover_league_lists_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FPL_ENTRY_ID", "2282251")
        self._mock_entry(
            2282251,
            [
                {"id": 314, "name": "Overall", "league_type": "s"},
                {"id": 555001, "name": "The Office", "league_type": "x"},
            ],
        )
        result = runner.invoke(app, ["--data-root", str(tmp_path), "discover-league"])
        assert result.exit_code == exit_codes.SUCCESS, result.output
        assert "555001\tThe Office" in result.output
        assert "Overall" not in result.output

    def test_discover_league_reports_an_empty_result_plainly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The state today: registered, but the league does not exist yet."""
        monkeypatch.setenv("FPL_ENTRY_ID", "2282251")
        self._mock_entry(2282251, [{"id": 314, "name": "Overall", "league_type": "s"}])
        result = runner.invoke(app, ["--data-root", str(tmp_path), "discover-league"])
        assert result.exit_code == exit_codes.SUCCESS
        assert "no private leagues yet" in result.output

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


class TestDatasetCommand:
    """CLI surface for Step 23: building ``data/training/matrix.parquet``."""

    def _facts(self, root: Path, season: str, rows: list[dict]) -> None:
        import polars as pl

        from fpl.storage.parquet_io import write_parquet

        columns = [
            "season",
            "fixture_id",
            "player_id",
            "player_code",
            "team_id",
            "team_code",
            "opponent_team_id",
            "opponent_team_code",
            "was_home",
            "kickoff_time",
            "event",
            "position",
            "minutes",
            "starts",
            "goals_scored",
            "assists",
            "goals_conceded",
            "own_goals",
            "penalties_saved",
            "penalties_missed",
            "yellow_cards",
            "red_cards",
            "saves",
            "cbi",
            "tackles",
            "recoveries",
            "defensive_contribution",
            "attempted_passes",
            "completed_passes",
            "key_passes",
            "big_chances_created",
            "big_chances_missed",
            "open_play_crosses",
            "dribbles",
            "tackled",
            "fouls",
            "offside",
            "target_missed",
            "errors_leading_to_goal",
            "errors_leading_to_goal_attempt",
            "penalties_conceded",
            "winning_goals",
            "expected_goals",
            "expected_assists",
            "expected_goal_involvements",
            "expected_goals_conceded",
            "total_points_fpl",
            "bonus_fpl",
            "bps_fpl",
            "obs_defensive",
            "obs_bps_inputs",
            "obs_expected",
            "obs_starts",
        ]
        full_rows = []
        for row in rows:
            full_row: dict = dict.fromkeys(columns, 0)
            full_row.update(
                {
                    "season": season,
                    "player_code": "code-1",
                    "was_home": True,
                    "position": "MID",
                    "obs_defensive": True,
                    "obs_bps_inputs": True,
                    "obs_expected": True,
                    "obs_starts": True,
                }
            )
            full_row.update(row)
            full_rows.append(full_row)

        frame = pl.DataFrame(full_rows).with_columns(
            pl.col("kickoff_time").str.strptime(pl.Datetime(time_unit="us", time_zone="UTC"))
        )
        out_dir = root / "facts" / "player_fixture" / f"season={season}"
        out_dir.mkdir(parents=True, exist_ok=True)
        write_parquet(frame, out_dir / "part.parquet")

    def test_no_facts_built_reports_nothing_to_build(self, isolated_data_root: Path) -> None:
        result = runner.invoke(app, ["--data-root", str(isolated_data_root), "dataset"])
        assert result.exit_code == exit_codes.SUCCESS
        assert "skipped" in result.output

    def test_builds_matrix_from_available_season(self, isolated_data_root: Path) -> None:
        self._facts(
            isolated_data_root,
            "2016-17",
            [
                {
                    "fixture_id": 1,
                    "player_id": 1,
                    "event": 1,
                    "kickoff_time": "2016-08-13T14:00:00",
                    "minutes": 90,
                }
            ],
        )

        result = runner.invoke(app, ["--data-root", str(isolated_data_root), "dataset"])

        assert result.exit_code == exit_codes.SUCCESS
        assert "1 row(s) across 1 season(s)" in result.output
        matrix_path = isolated_data_root / "training" / "matrix.parquet"
        assert matrix_path.is_file()


class TestBackfillEloCommand:
    """CLI surface for the historical Club Elo backfill (plan §0.6, Step 14).

    The command wraps a ~1,150-request, two-hour run, so the cheap safety
    properties — costing it before starting, refusing an empty season range,
    and not touching the network on a dry run — are worth pinning.
    """

    def _facts(self, root: Path, kickoffs: list[str], season: str = "2016-17") -> None:
        import polars as pl

        from fpl.storage.parquet_io import write_parquet

        out_dir = root / "facts" / "player_fixture" / f"season={season}"
        out_dir.mkdir(parents=True, exist_ok=True)
        frame = pl.DataFrame(
            {"fixture_id": list(range(1, len(kickoffs) + 1)), "kickoff_time": kickoffs}
        ).with_columns(
            pl.col("kickoff_time").str.strptime(pl.Datetime(time_unit="us", time_zone="UTC"))
        )
        write_parquet(frame, out_dir / "part.parquet")

    def test_dry_run_costs_the_work_without_fetching(self, isolated_data_root: Path) -> None:
        """No network call is mocked here, so a dry run that touched the
        network would fail rather than pass quietly."""
        self._facts(isolated_data_root, ["2016-08-13T14:00:00Z", "2016-08-20T14:00:00Z"])

        result = runner.invoke(
            app,
            [
                "--data-root",
                str(isolated_data_root),
                "backfill-elo",
                "--from",
                "2016-17",
                "--to",
                "2016-17",
                "--dry-run",
            ],
        )

        assert result.exit_code == exit_codes.SUCCESS
        assert "2 date(s) in scope" in result.output
        assert "2 to fetch" in result.output

    def test_dry_run_with_no_facts_reports_nothing_to_do(self, isolated_data_root: Path) -> None:
        result = runner.invoke(
            app,
            [
                "--data-root",
                str(isolated_data_root),
                "backfill-elo",
                "--from",
                "2016-17",
                "--to",
                "2016-17",
                "--dry-run",
            ],
        )

        assert result.exit_code == exit_codes.SUCCESS
        assert "0 date(s) in scope" in result.output

    def test_empty_season_range_is_rejected(self, isolated_data_root: Path) -> None:
        result = runner.invoke(
            app,
            [
                "--data-root",
                str(isolated_data_root),
                "backfill-elo",
                "--from",
                "2024-25",
                "--to",
                "2016-17",
            ],
        )

        assert result.exit_code == exit_codes.USAGE
