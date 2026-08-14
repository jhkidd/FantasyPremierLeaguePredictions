"""Ground-truth tests for the two derivations that stand in for files the
archive never published.

Vaastav ships ``fixtures.csv`` from 2018/19 and ``teams.csv`` from 2019/20;
the FPL API serves only the current season. So for the earliest seasons both
tables are *derived* — the fixture calendar from ``player_fixture_stats``, and
the ``team_id -> code`` mapping from aligning that calendar against
football-data.co.uk's own record of the same 380 matches.

Derived data is only trustworthy if the derivation is falsifiable, and here it
is: every season from 2018/19 onward has the real file committed alongside,
so the same code can be run against seasons whose answer is already known and
required to reproduce it exactly. These tests are that check.

Like ``test_reconciliation.py`` this runs against the real committed ``data/``
tree rather than a synthetic fixture, and skips (rather than fails) a season
that has not been staged yet.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from fpl.config import Season
from fpl.identity.teams_from_matches import derive_teams
from fpl.staging.fixtures_from_facts import fixtures_from_player_stats
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet
from fpl.storage.raw_io import read_raw
from fpl.staging.vaastav import stage_fixtures_csv

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"

_TEAMS_GROUND_TRUTH = [Season(year) for year in range(2019, 2026)]
_FIXTURES_GROUND_TRUTH = [Season(year) for year in range(2018, 2026)]

_EXPECTED_CLUBS = 20


def _staged(table: str, season: Season) -> Path:
    return paths.staged_table(table, season, data_root=DATA_ROOT) / "part.parquet"


@pytest.mark.parametrize("season", _TEAMS_GROUND_TRUTH, ids=str)
def test_derived_teams_reproduce_the_published_teams_csv(season: Season) -> None:
    """The derivation must recover all 20 ``team_id -> code`` pairs exactly.

    This is what licenses using it for 2016/17-2018/19, where no answer key
    exists. An approximate match would not be good enough: a single wrong
    club silently attaches another team's Elo and odds to every one of its
    38 fixtures.
    """
    raw_teams = paths.latest_partition("vaastav", "teams", season, data_root=DATA_ROOT)
    if raw_teams is None or not _staged("footballdata_matches_and_odds", season).exists():
        pytest.skip(f"{season} not fully staged")

    derived = derive_teams(season, data_root=DATA_ROOT)
    assert derived is not None
    frame, _report = derived

    truth = read_parquet(_staged("teams", season))
    expected = {team_id: int(code) for team_id, code in truth.select("team_id", "code").iter_rows()}
    actual = {team_id: int(code) for team_id, code in frame.select("team_id", "code").iter_rows()}

    assert len(expected) == _EXPECTED_CLUBS
    assert actual == expected


@pytest.mark.parametrize("season", _TEAMS_GROUND_TRUTH, ids=str)
def test_every_fixture_aligns_to_a_football_data_match(season: Season) -> None:
    """No fixture may be left unaligned — an unaligned one is a club losing
    votes, which is how a mapping silently becomes ambiguous."""
    if not _staged("footballdata_matches_and_odds", season).exists():
        pytest.skip(f"{season} not fully staged")

    derived = derive_teams(season, data_root=DATA_ROOT)
    assert derived is not None
    _frame, report = derived

    assert report.excluded["fixtures_unaligned"] == 0


@pytest.mark.parametrize("season", _FIXTURES_GROUND_TRUTH, ids=str)
def test_reconstructed_fixtures_reproduce_the_published_fixtures_csv(season: Season) -> None:
    """Reconstructing from player rows must give back the real fixture list.

    Checked across three schema eras, since the columns the reconstruction
    leans on (``was_home``, ``opponent_team``, the repeated scorelines) are
    exactly the ones the archive kept changing.

    Identity and result are required to match exactly. ``kickoff_time`` is
    required only to the calendar day: ``merged_gw.csv`` records the time as
    it stood when the gameweek was played while ``fixtures.csv`` is an
    end-of-season snapshot, so a rescheduled match can legitimately disagree
    by minutes between the two (fixture 263 of 2021/22 moved by 30). The day
    is what downstream actually consumes — Elo is looked up at T-1 and
    congestion windows are counted in days.
    """
    partition = paths.latest_partition("vaastav", "fixtures", season, data_root=DATA_ROOT)
    if partition is None or not _staged("player_fixture_stats", season).exists():
        pytest.skip(f"{season} not fully staged")

    truth, _ = stage_fixtures_csv(read_raw(partition)[0], season)
    stats = read_parquet(_staged("player_fixture_stats", season))
    rebuilt, _report = fixtures_from_player_stats(stats, season)

    exact = ["fixture_id", "event", "team_h", "team_a", "team_h_score", "team_a_score"]
    truth_rows = truth.select(exact).sort("fixture_id")
    rebuilt_rows = rebuilt.select(exact).sort("fixture_id")

    assert rebuilt_rows.height == truth_rows.height
    assert rebuilt_rows.equals(truth_rows)

    as_day = pl.col("kickoff_time").str.slice(0, 10)
    truth_days = truth.sort("fixture_id").select(as_day)["kickoff_time"]
    rebuilt_days = rebuilt.sort("fixture_id").select(as_day)["kickoff_time"]
    assert truth_days.to_list() == rebuilt_days.to_list()


@pytest.mark.parametrize("season", [Season(y) for y in range(2016, 2026)], ids=str)
def test_every_season_resolves_a_full_set_of_clubs(season: Season) -> None:
    """Including the three seasons with no published ``teams.csv`` at all —
    without these, ``facts/team_fixture`` cannot be built for them."""
    if not _staged("teams", season).exists():
        pytest.skip(f"{season} teams not staged")

    teams = read_parquet(_staged("teams", season))

    assert teams.height == _EXPECTED_CLUBS
    assert teams["code"].null_count() == 0
    assert teams["code"].n_unique() == _EXPECTED_CLUBS
    assert teams["team_id"].n_unique() == _EXPECTED_CLUBS


def test_team_id_is_not_stable_across_seasons_but_code_is() -> None:
    """Pins the reason ``code`` has to exist at all (plan §0.4).

    FPL reassigns ``team_id`` each season, so any cross-season team feature
    keyed on it silently compares different clubs.
    """
    staged = [s for s in (Season(y) for y in range(2016, 2026)) if _staged("teams", s).exists()]
    if len(staged) < 2:
        pytest.skip("need at least two staged seasons")

    frames = [
        read_parquet(_staged("teams", season)).select("season", "team_id", "code")
        for season in staged
    ]
    combined = pl.concat(frames)

    per_team_id = combined.group_by("team_id").agg(pl.col("code").n_unique().alias("codes"))
    assert per_team_id.filter(pl.col("codes") > 1).height > 0, (
        "expected at least one team_id to map to different clubs in different seasons"
    )
