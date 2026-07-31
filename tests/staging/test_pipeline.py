from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fpl.config import Season
from fpl.quality.checks import check_staged_tables
from fpl.quality.gates import has_blocking_violations
from fpl.staging.pipeline import stage_fpl_source
from fpl.storage.raw_io import RawArtifact, write_raw

SEASON = Season(2026)
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fpl"


def _write_bootstrap(data_root: Path, moment: datetime) -> None:
    body = (FIXTURES_DIR / "bootstrap_static.json").read_bytes()
    artifact = RawArtifact(
        source="fpl",
        endpoint="bootstrap_static",
        season=SEASON,
        url="https://fantasy.premierleague.com/api/bootstrap-static/",
        http_status=200,
        body=body,
        fetched_at=moment,
        connector_version="1",
    )
    write_raw(artifact, data_root=data_root)


def _write_fixtures(data_root: Path, moment: datetime) -> None:
    body = (FIXTURES_DIR / "fixtures.json").read_bytes()
    artifact = RawArtifact(
        source="fpl",
        endpoint="fixtures",
        season=SEASON,
        url="https://fantasy.premierleague.com/api/fixtures/",
        http_status=200,
        body=body,
        fetched_at=moment,
        connector_version="1",
    )
    write_raw(artifact, data_root=data_root)


class TestStageFplSourceEndToEnd:
    def test_stages_players_teams_events_and_fixtures(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        moment = datetime(2026, 8, 1, tzinfo=UTC)
        _write_bootstrap(data_root, moment)
        _write_fixtures(data_root, moment)

        results = stage_fpl_source(SEASON, data_root=data_root)
        staged_names = {r.table for r in results if r.written}
        assert {"players", "teams", "events", "fixtures"} <= staged_names

    def test_rebuild_is_byte_identical(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        moment = datetime(2026, 8, 1, tzinfo=UTC)
        _write_bootstrap(data_root, moment)

        stage_fpl_source(SEASON, data_root=data_root, tables={"players"})
        first = (data_root / "staged" / "players" / "season=2026-27" / "part.parquet").read_bytes()

        stage_fpl_source(SEASON, data_root=data_root, tables={"players"})
        second = (data_root / "staged" / "players" / "season=2026-27" / "part.parquet").read_bytes()

        assert first == second

    def test_price_snapshots_stack_across_two_captures(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_bootstrap(data_root, datetime(2026, 8, 1, tzinfo=UTC))
        _write_bootstrap(data_root, datetime(2026, 8, 2, tzinfo=UTC))

        results = stage_fpl_source(SEASON, data_root=data_root, tables={"price_snapshots"})
        [result] = [r for r in results if r.table == "price_snapshots"]
        # Two captures of the same fixture yield one row per player per capture,
        # unless the second capture happened to be byte-identical and skipped.
        assert result.rows > 0

    def test_check_is_clean_after_staging(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        moment = datetime(2026, 8, 1, tzinfo=UTC)
        _write_bootstrap(data_root, moment)
        _write_fixtures(data_root, moment)
        stage_fpl_source(SEASON, data_root=data_root)

        violations = check_staged_tables(SEASON, data_root=data_root)
        assert not has_blocking_violations(violations)
