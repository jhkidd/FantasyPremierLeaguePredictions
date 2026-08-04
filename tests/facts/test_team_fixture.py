from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from fpl.config import Season
from fpl.facts.team_fixture import (
    KEY,
    build_team_fixture_facts,
    write_team_fixture_facts,
)
from fpl.identity.team_external_ids import write_team_external_ids
from fpl.staging.pipeline import (
    stage_clubelo_source,
    stage_footballdata_source,
    stage_fpl_source,
    stage_openfootball_source,
)
from fpl.storage.raw_io import RawArtifact, write_raw

SEASON = Season(2025)

# Two FPL teams, ids 1 (Arsenal, team_code=3) and 2 (Bournemouth, team_code=90),
# one fixture between them.
_BOOTSTRAP_BODY = json.dumps(
    {
        "teams": [
            {"id": 1, "code": 3, "name": "Arsenal", "short_name": "ARS", "strength": 4},
            {"id": 2, "code": 90, "name": "Bournemouth", "short_name": "BOU", "strength": 3},
        ],
        "elements": [
            {
                "id": 1,
                "code": 100001,
                "team": 1,
                "element_type": 1,
                "first_name": "Test",
                "second_name": "Player",
                "web_name": "Player",
                "status": "a",
                "now_cost": 50,
                "selected_by_percent": "1.0",
                "total_points": 0,
                "minutes": 0,
                "goals_scored": 0,
                "assists": 0,
                "clean_sheets": 0,
                "goals_conceded": 0,
                "own_goals": 0,
                "penalties_saved": 0,
                "penalties_missed": 0,
                "yellow_cards": 0,
                "red_cards": 0,
                "saves": 0,
                "bonus": 0,
                "bps": 0,
            }
        ],
        "events": [
            {
                "id": 1,
                "name": "Gameweek 1",
                "deadline_time": "2025-08-15T17:30:00Z",
                "finished": True,
                "is_current": False,
                "is_next": False,
                "is_previous": True,
            }
        ],
    }
).encode()

_FIXTURES_BODY = json.dumps(
    [
        {
            "id": 501,
            "code": 1000501,
            "event": 1,
            "kickoff_time": "2025-08-16T14:00:00Z",
            "team_h": 1,
            "team_a": 2,
            "team_h_score": 2,
            "team_a_score": 1,
            "finished": True,
            "minutes": 90,
        }
    ]
).encode()

_TEAM_EXTERNAL_IDS = pl.DataFrame(
    {
        "team_code": ["3", "90"],
        "clubelo_name": ["Arsenal", "Bournemouth"],
        "understat_name": [None, None],
        "footballdata_couk_name": ["Arsenal", "Bournemouth"],
        "openfootball_name": ["Arsenal FC", "Bournemouth AFC"],
    }
)

_CLUBELO_HEADER = "Rank,Club,Country,Level,Elo,From,To\n"
_CLUBELO_ROW_T_MINUS_1 = (
    "1,Arsenal,ENG,1,1900.0,2025-08-10,2025-08-16\n"
    "2,Bournemouth,ENG,1,1650.0,2025-08-10,2025-08-16\n"
)
_CLUBELO_ROW_SAME_DAY = (
    "1,Arsenal,ENG,1,1950.0,2025-08-16,2025-08-23\n"
    "2,Bournemouth,ENG,1,1600.0,2025-08-16,2025-08-23\n"
)

_FOOTBALLDATA_HEADER = "Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,B365H,B365D,B365A\n"
_FOOTBALLDATA_ROW = "16/08/2025,Arsenal,Bournemouth,2,1,H,1.5,4.0,6.0\n"

_OPENFOOTBALL_LINES = [
    "= UEFA Champions League 2025/26",
    "",
    chr(0x25AA) + " League, Matchday 1",
    "Sat Aug 9 2025",
    "18:45  Arsenal FC (ENG)  v  Some Foreign Club (ESP)  3-0 (1-0)",
    "Tue Aug 12 2025",
    "18:45  Another Foreign Club (ITA)  v  Bournemouth AFC (ENG)  1-1 (0-0)",
    "",
]
_OPENFOOTBALL_BODY = "\n".join(_OPENFOOTBALL_LINES).encode()


def _write_raw(
    data_root: Path,
    source: str,
    endpoint: str,
    body: bytes,
    *,
    content_type: str = "json",
    fetched_at: datetime = datetime(2026, 8, 4, tzinfo=UTC),
) -> None:
    artifact = RawArtifact(
        source=source,
        endpoint=endpoint,
        season=SEASON,
        url=f"https://example.invalid/{source}/{endpoint}",
        http_status=200,
        body=body,
        fetched_at=fetched_at,
        connector_version="1",
        content_type=content_type,
    )
    write_raw(artifact, data_root=data_root)


def _write_clubelo(data_root: Path, *, same_day: bool = False) -> None:
    """Two Club Elo captures: one T-1 (the day before kickoff), one same-day
    as kickoff - used to prove the join picks the T-1 rating, never the
    same-day-or-later one."""
    body = (_CLUBELO_HEADER + _CLUBELO_ROW_T_MINUS_1).encode()
    _write_raw(
        data_root,
        "clubelo",
        "ratings",
        body,
        content_type="csv",
        fetched_at=datetime(2025, 8, 15, tzinfo=UTC),
    )
    if same_day:
        body = (_CLUBELO_HEADER + _CLUBELO_ROW_SAME_DAY).encode()
        _write_raw(
            data_root,
            "clubelo",
            "ratings",
            body,
            content_type="csv",
            fetched_at=datetime(2025, 8, 16, tzinfo=UTC),
        )


def _write_footballdata(data_root: Path) -> None:
    body = (_FOOTBALLDATA_HEADER + _FOOTBALLDATA_ROW).encode()
    _write_raw(data_root, "footballdata", "matches_and_odds", body, content_type="csv")


def _write_openfootball(data_root: Path) -> None:
    _write_raw(
        data_root, "openfootball", "champions_league", _OPENFOOTBALL_BODY, content_type="txt"
    )


def _stage_everything(data_root: Path, *, include_openfootball: bool = True) -> None:
    stage_fpl_source(SEASON, data_root=data_root, tables={"teams", "fixtures"})
    stage_clubelo_source(SEASON, data_root=data_root)
    stage_footballdata_source(SEASON, data_root=data_root)
    if include_openfootball:
        stage_openfootball_source(SEASON, data_root=data_root)
    write_team_external_ids(_TEAM_EXTERNAL_IDS, data_root=data_root)


def _write_fpl_bootstrap_and_fixtures(data_root: Path) -> None:
    _write_raw(data_root, "fpl", "bootstrap_static", _BOOTSTRAP_BODY)
    _write_raw(data_root, "fpl", "fixtures", _FIXTURES_BODY)


class TestBuildTeamFixtureFacts:
    def test_no_staged_data_returns_none(self, tmp_path: Path) -> None:
        assert build_team_fixture_facts(SEASON, data_root=tmp_path / "data") is None

    def test_builds_two_rows_per_fixture(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_fpl_bootstrap_and_fixtures(data_root)
        _write_clubelo(data_root)
        _write_footballdata(data_root)
        _write_openfootball(data_root)
        _stage_everything(data_root)

        facts = build_team_fixture_facts(SEASON, data_root=data_root)
        assert facts is not None
        assert facts.height == 2
        assert facts.select(list(KEY)).is_duplicated().sum() == 0

    def test_elo_join_picks_t_minus_1_not_same_day(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_fpl_bootstrap_and_fixtures(data_root)
        _write_clubelo(data_root, same_day=True)
        _write_footballdata(data_root)
        _write_openfootball(data_root)
        _stage_everything(data_root)

        facts = build_team_fixture_facts(SEASON, data_root=data_root)
        arsenal_row = facts.filter(pl.col("team_id") == 1).row(0, named=True)
        # 1900.0 is the T-1 (2025-08-15) rating; 1950.0 is the same-day
        # (2025-08-16) rating that must never be picked.
        assert arsenal_row["elo_rating"] == pytest.approx(1900.0)
        assert arsenal_row["opponent_elo_rating"] == pytest.approx(1650.0)

    def test_odds_implied_probabilities_are_overround_normalised(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_fpl_bootstrap_and_fixtures(data_root)
        _write_clubelo(data_root)
        _write_footballdata(data_root)
        _write_openfootball(data_root)
        _stage_everything(data_root)

        facts = build_team_fixture_facts(SEASON, data_root=data_root)
        arsenal_row = facts.filter(pl.col("team_id") == 1).row(0, named=True)
        raw_home = 1 / 1.5
        raw_draw = 1 / 4.0
        raw_away = 1 / 6.0
        overround = raw_home + raw_draw + raw_away
        assert arsenal_row["odds_implied_win_prob"] == pytest.approx(raw_home / overround)
        assert arsenal_row["odds_implied_draw_prob"] == pytest.approx(raw_draw / overround)
        assert arsenal_row["odds_implied_loss_prob"] == pytest.approx(raw_away / overround)
        # The three implied probabilities must sum to 1 once the overround
        # (bookmaker margin) has been removed.
        total = (
            arsenal_row["odds_implied_win_prob"]
            + arsenal_row["odds_implied_draw_prob"]
            + arsenal_row["odds_implied_loss_prob"]
        )
        assert total == pytest.approx(1.0)

        bournemouth_row = facts.filter(pl.col("team_id") == 2).row(0, named=True)
        # Bournemouth is away, so its "win" is Arsenal's "loss" and vice versa.
        assert bournemouth_row["odds_implied_win_prob"] == pytest.approx(raw_away / overround)
        assert bournemouth_row["odds_implied_loss_prob"] == pytest.approx(raw_home / overround)

    def test_fixture_congestion_counts_strictly_before_kickoff(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_fpl_bootstrap_and_fixtures(data_root)
        _write_clubelo(data_root)
        _write_footballdata(data_root)
        _write_openfootball(data_root)
        _stage_everything(data_root)

        facts = build_team_fixture_facts(SEASON, data_root=data_root)
        arsenal_row = facts.filter(pl.col("team_id") == 1).row(0, named=True)
        # Arsenal's Champions League fixture (9 Aug 2025) is within 7/14/28
        # days before the 16 Aug 2025 kickoff, and is the only prior fixture.
        assert arsenal_row["fixture_count_prior_7_days"] == 1
        assert arsenal_row["fixture_count_prior_14_days"] == 1
        assert arsenal_row["fixture_count_prior_28_days"] == 1

        bournemouth_row = facts.filter(pl.col("team_id") == 2).row(0, named=True)
        # Bournemouth's Champions League fixture (12 Aug 2025) is also prior.
        assert bournemouth_row["fixture_count_prior_7_days"] == 1
        assert bournemouth_row["fixture_count_prior_14_days"] == 1
        assert bournemouth_row["fixture_count_prior_28_days"] == 1

    def test_missing_openfootball_source_leaves_row_with_null_not_dropped(
        self, tmp_path: Path
    ) -> None:
        data_root = tmp_path / "data"
        _write_fpl_bootstrap_and_fixtures(data_root)
        _write_clubelo(data_root)
        _write_footballdata(data_root)
        # Deliberately no openfootball capture at all for this season.
        _stage_everything(data_root, include_openfootball=False)

        facts = build_team_fixture_facts(SEASON, data_root=data_root)
        assert facts is not None
        assert facts.height == 2
        # Congestion still counts FPL's own fixtures (zero prior ones here),
        # never null and never a dropped row, even with no European data.
        arsenal_row = facts.filter(pl.col("team_id") == 1).row(0, named=True)
        assert arsenal_row["fixture_count_prior_7_days"] == 0
        assert arsenal_row["elo_rating"] is not None

    def test_missing_clubelo_source_leaves_row_with_null_not_dropped(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_fpl_bootstrap_and_fixtures(data_root)
        _write_footballdata(data_root)
        _write_openfootball(data_root)
        _stage_everything(data_root)

        facts = build_team_fixture_facts(SEASON, data_root=data_root)
        assert facts is not None
        assert facts.height == 2
        row = facts.row(0, named=True)
        assert row["elo_rating"] is None
        assert row["opponent_elo_rating"] is None

    def test_duplicate_key_raises(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        # Duplicate fixtures payload (same fixture id twice) forces a key
        # collision once joined with team_id.
        duplicated_fixtures_body = json.dumps(
            [
                {
                    "id": 501,
                    "code": 1000501,
                    "event": 1,
                    "kickoff_time": "2025-08-16T14:00:00Z",
                    "team_h": 1,
                    "team_a": 2,
                    "team_h_score": 2,
                    "team_a_score": 1,
                    "finished": True,
                    "minutes": 90,
                },
                {
                    "id": 501,
                    "code": 1000501,
                    "event": 1,
                    "kickoff_time": "2025-08-16T14:00:00Z",
                    "team_h": 1,
                    "team_a": 2,
                    "team_h_score": 2,
                    "team_a_score": 1,
                    "finished": True,
                    "minutes": 90,
                },
            ]
        ).encode()
        _write_raw(data_root, "fpl", "bootstrap_static", _BOOTSTRAP_BODY)
        _write_raw(data_root, "fpl", "fixtures", duplicated_fixtures_body)
        write_team_external_ids(_TEAM_EXTERNAL_IDS, data_root=data_root)
        stage_fpl_source(SEASON, data_root=data_root, tables={"teams", "fixtures"})

        with pytest.raises(ValueError, match="not unique"):
            build_team_fixture_facts(SEASON, data_root=data_root)

    def test_unresolved_team_name_is_reported_not_silently_dropped(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_fpl_bootstrap_and_fixtures(data_root)
        _write_clubelo(data_root)
        _write_footballdata(data_root)
        _write_openfootball(data_root)
        stage_fpl_source(SEASON, data_root=data_root, tables={"teams", "fixtures"})
        stage_clubelo_source(SEASON, data_root=data_root)
        stage_footballdata_source(SEASON, data_root=data_root)
        stage_openfootball_source(SEASON, data_root=data_root)
        # No crosswalk written at all -> every source name is unresolved.
        write_team_external_ids(
            pl.DataFrame(
                {
                    "team_code": [],
                    "clubelo_name": [],
                    "understat_name": [],
                    "footballdata_couk_name": [],
                    "openfootball_name": [],
                },
                schema={
                    "team_code": pl.Utf8,
                    "clubelo_name": pl.Utf8,
                    "understat_name": pl.Utf8,
                    "footballdata_couk_name": pl.Utf8,
                    "openfootball_name": pl.Utf8,
                },
            ),
            data_root=data_root,
        )

        result = write_team_fixture_facts(SEASON, data_root=data_root)
        assert result.unresolved_teams
        assert "Arsenal" in result.unresolved_teams


class TestWriteTeamFixtureFacts:
    def test_writes_parquet_and_reports_row_count(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_fpl_bootstrap_and_fixtures(data_root)
        _write_clubelo(data_root)
        _write_footballdata(data_root)
        _write_openfootball(data_root)
        _stage_everything(data_root)

        result = write_team_fixture_facts(SEASON, data_root=data_root)
        assert result.written
        assert result.frame.height == 2
        out_path = data_root / "facts" / "team_fixture" / "season=2025-26" / "part.parquet"
        assert out_path.exists()

    def test_rebuild_is_byte_identical(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_fpl_bootstrap_and_fixtures(data_root)
        _write_clubelo(data_root)
        _write_footballdata(data_root)
        _write_openfootball(data_root)
        _stage_everything(data_root)
        out_path = data_root / "facts" / "team_fixture" / "season=2025-26" / "part.parquet"

        write_team_fixture_facts(SEASON, data_root=data_root)
        first = out_path.read_bytes()
        write_team_fixture_facts(SEASON, data_root=data_root)
        second = out_path.read_bytes()
        assert first == second

    def test_no_staged_data_reports_not_written(self, tmp_path: Path) -> None:
        result = write_team_fixture_facts(SEASON, data_root=tmp_path / "data")
        assert not result.written
