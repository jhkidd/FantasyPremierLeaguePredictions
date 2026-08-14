"""Regression tests for ``team_id`` derivation (plan §0.3, Phase 0 Steps 1-4).

``merged_gw.csv`` carries the player's own team inconsistently across schema
eras: E1-E3 resolve a numeric ``team_id`` from ``players_raw.csv`` (an
*end-of-season* snapshot, so mid-season transfers are misattributed), while
E4+ carry only a team *name* string that needs a staged ``teams`` table to
resolve — a table only ever built for the current season, so six seasons
silently staged 100% null.

Both failure modes are repaired by the same invariant: a fixture's two teams
are exactly the two distinct ``opponent_team_id`` values recorded against it,
so a row's own team is whichever of the two is not that row's opponent. This
needs no external table and was verified against all 3,800 fixtures across ten
seasons without exception.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from fpl.config import Season
from fpl.facts.player_fixture import build_player_fixture_facts
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet

SEASON = Season(2021)

_HOME_TEAM_ID = 17
_AWAY_TEAM_ID = 19


def _stats_row(
    *,
    player_id: int,
    fixture_id: int,
    opponent_team_id: int,
    was_home: bool,
    team_name: str,
) -> dict:
    """One staged ``player_fixture_stats`` row, E4+ shaped.

    E4+ is the era that carries ``team`` as a name and no ``team_id`` at all,
    which is the shape that produced the all-null seasons.
    """
    return {
        "season": str(SEASON),
        "player_name": f"Player {player_id}",
        "position": "MID",
        "team": team_name,
        "player_id": player_id,
        "fixture_id": fixture_id,
        "event": 1,
        "kickoff_time": "2021-08-14T14:00:00Z",
        "was_home": was_home,
        "opponent_team": opponent_team_id,
        "minutes": 90,
        "starts": 1,
        "goals_scored": 0,
        "assists": 0,
        "goals_conceded": 0,
        "own_goals": 0,
        "penalties_saved": 0,
        "penalties_missed": 0,
        "yellow_cards": 0,
        "red_cards": 0,
        "saves": 0,
        "bonus_fpl": 0,
        "bps_fpl": 10,
        "total_points_fpl": 2,
    }


def _write_stats(data_root: Path, rows: list[dict]) -> None:
    out_dir = paths.staged_table("player_fixture_stats", SEASON, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(pl.DataFrame(rows), out_dir / "part.parquet")


def _two_team_fixture(fixture_id: int = 100) -> list[dict]:
    """A realistic fixture: both sides present, each naming the other."""
    return [
        _stats_row(
            player_id=1,
            fixture_id=fixture_id,
            opponent_team_id=_AWAY_TEAM_ID,
            was_home=True,
            team_name="Home FC",
        ),
        _stats_row(
            player_id=2,
            fixture_id=fixture_id,
            opponent_team_id=_AWAY_TEAM_ID,
            was_home=True,
            team_name="Home FC",
        ),
        _stats_row(
            player_id=3,
            fixture_id=fixture_id,
            opponent_team_id=_HOME_TEAM_ID,
            was_home=False,
            team_name="Away FC",
        ),
    ]


class TestTeamIdDerivation:
    def test_team_id_derived_from_fixture_invariant(self, tmp_path: Path) -> None:
        """The headline repair: no ``team_id`` column, no staged ``teams``
        table, yet every row still resolves to the opposite team."""
        data_root = tmp_path / "data"
        _write_stats(data_root, _two_team_fixture())

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        assert facts is not None
        assert facts["team_id"].null_count() == 0
        by_player = {row["player_id"]: row for row in facts.iter_rows(named=True)}
        assert by_player[1]["team_id"] == _HOME_TEAM_ID
        assert by_player[2]["team_id"] == _HOME_TEAM_ID
        assert by_player[3]["team_id"] == _AWAY_TEAM_ID

    def test_no_row_is_its_own_opponent(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        _write_stats(data_root, _two_team_fixture())

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        assert facts.filter(pl.col("team_id") == pl.col("opponent_team_id")).height == 0

    def test_each_fixture_is_derived_independently(self, tmp_path: Path) -> None:
        """Derivation must be per fixture, not global — a team's id in one
        fixture says nothing about an unrelated fixture."""
        data_root = tmp_path / "data"
        other = [
            _stats_row(
                player_id=4,
                fixture_id=200,
                opponent_team_id=5,
                was_home=True,
                team_name="Home FC",
            ),
            _stats_row(
                player_id=5,
                fixture_id=200,
                opponent_team_id=_HOME_TEAM_ID,
                was_home=False,
                team_name="Third FC",
            ),
        ]
        _write_stats(data_root, _two_team_fixture() + other)

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        by_player = {row["player_id"]: row for row in facts.iter_rows(named=True)}
        assert by_player[4]["team_id"] == _HOME_TEAM_ID
        assert by_player[5]["team_id"] == 5


class TestEarlyEraTeamIdIsCorrected:
    """Pins the 2016-17 to 2019-20 repair (plan §0.3).

    Those eras *do* carry a ``team_id``, sourced from ``players_raw.csv`` —
    an end-of-season snapshot, so every player transferred mid-season is
    attributed to the club they finished at rather than the one they played
    the fixture for. Fixture 3 of 2016-17 ended up with seven distinct
    ``team_id`` values for a two-team match. The incoming column must
    therefore be overwritten, never trusted.
    """

    def test_incoming_team_id_disagreeing_with_invariant_is_overwritten(
        self, tmp_path: Path
    ) -> None:
        data_root = tmp_path / "data"
        rows = _two_team_fixture()
        # A transferred player: the snapshot names the club he ended the
        # season at, not the one he played this fixture for.
        rows[0]["team_id"] = 42
        rows[1]["team_id"] = _HOME_TEAM_ID
        rows[2]["team_id"] = _AWAY_TEAM_ID
        _write_stats(data_root, rows)

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        by_player = {row["player_id"]: row for row in facts.iter_rows(named=True)}
        assert by_player[1]["team_id"] == _HOME_TEAM_ID, "stale snapshot value must be corrected"
        assert by_player[2]["team_id"] == _HOME_TEAM_ID
        assert by_player[3]["team_id"] == _AWAY_TEAM_ID

    def test_fixture_resolves_to_exactly_two_teams_after_repair(self, tmp_path: Path) -> None:
        """The observable symptom of the bug — more than two teams in one
        fixture — must be gone."""
        data_root = tmp_path / "data"
        rows = _two_team_fixture()
        for index, bogus in enumerate((42, 77, 91)):
            rows[index]["team_id"] = bogus
        _write_stats(data_root, rows)

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        assert facts.filter(pl.col("fixture_id") == 100)["team_id"].n_unique() == 2


class TestTeamIdDerivationEdgeCases:
    def test_single_sided_fixture_leaves_team_id_null(self, tmp_path: Path) -> None:
        """Only one side present: the own team is genuinely unknowable, so the
        column is left null for the quality gate to catch, rather than guessed."""
        data_root = tmp_path / "data"
        _write_stats(
            data_root,
            [
                _stats_row(
                    player_id=1,
                    fixture_id=100,
                    opponent_team_id=_AWAY_TEAM_ID,
                    was_home=True,
                    team_name="Home FC",
                )
            ],
        )

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        assert facts.height == 1
        assert facts.row(0, named=True)["team_id"] is None

    def test_three_distinct_opponents_raises(self, tmp_path: Path) -> None:
        """Three teams in one fixture is impossible; it means the source is
        corrupt and must surface loudly rather than resolve arbitrarily."""
        data_root = tmp_path / "data"
        rows = _two_team_fixture()
        rows.append(
            _stats_row(
                player_id=9,
                fixture_id=100,
                opponent_team_id=42,
                was_home=False,
                team_name="Interloper FC",
            )
        )
        _write_stats(data_root, rows)

        with pytest.raises(ValueError, match="fixture 100"):
            build_player_fixture_facts(SEASON, data_root=data_root)

    def test_null_opponent_ids_are_ignored_when_collecting_teams(self, tmp_path: Path) -> None:
        """A null opponent must not count as a third team."""
        data_root = tmp_path / "data"
        rows = _two_team_fixture()
        rows.append(
            _stats_row(
                player_id=9,
                fixture_id=100,
                opponent_team_id=None,
                was_home=False,
                team_name="Away FC",
            )
        )
        _write_stats(data_root, rows)

        facts = build_player_fixture_facts(SEASON, data_root=data_root)

        by_player = {row["player_id"]: row for row in facts.iter_rows(named=True)}
        assert by_player[1]["team_id"] == _HOME_TEAM_ID
        assert by_player[9]["team_id"] is None
