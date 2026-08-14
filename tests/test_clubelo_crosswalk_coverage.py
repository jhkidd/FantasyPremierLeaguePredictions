"""The ClubElo name crosswalk covers every club, not just the current twenty.

Discovered during Phase 0 Step 16. `clubelo_name` had been filled in only for
the clubs in the *current* Premier League, so every relegated or
since-promoted club — Leicester, Southampton, Ipswich, Hull, Swansea and nine
others — resolved to no rating at all.

The failure was invisible in aggregate and only became visible once the
historical backfill made elo populated enough to look at: null rates came out
as exact multiples of 5% (40%, 35%, 25%, 20%, 15%, 10%), and 5% of a 760-row
season is exactly 38 rows — one club's entire campaign. Whole clubs were
missing, not scattered dates.

These tests pin the mapping to the club names Club Elo actually publishes, so
a future promoted club silently reintroducing the gap fails here rather than
degrading a season's features by 5% unnoticed.
"""

from __future__ import annotations

import csv
from pathlib import Path

import polars as pl
import pytest

from fpl.config import Season
from fpl.storage import paths

CROSSWALK = Path(__file__).resolve().parents[1] / "data" / "crosswalk" / "team_external_ids.csv"
DATA_ROOT = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not CROSSWALK.is_file(), reason="crosswalk not present in this checkout"
)


def _crosswalk_rows() -> list[dict[str, str]]:
    with CROSSWALK.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _clubelo_top_flight_names() -> set[str]:
    """Every club Club Elo has listed at level 1 across the staged captures.

    Level 1 is the Premier League, so this is exactly the set of clubs that
    played top-flight football in the seasons we hold — the set the crosswalk
    has to cover in full.
    """
    frames = []
    for year in range(2016, 2026):
        part = paths.staged_table("clubelo_ratings", Season(year), data_root=DATA_ROOT)
        part = part / "part.parquet"
        if part.is_file():
            frames.append(pl.read_parquet(part).select("club", "country", "level"))
    if not frames:
        pytest.skip("no staged clubelo_ratings in this checkout")
    combined = pl.concat(frames).unique()
    return set(
        combined.filter((pl.col("country") == "ENG") & (pl.col("level") == 1))["club"].to_list()
    )


class TestClubEloCrosswalkCoverage:
    def test_every_team_code_has_a_clubelo_name(self) -> None:
        """A blank here costs that club its elo for every season it played —
        silently, since the column simply comes back null."""
        blank = [row["team_code"] for row in _crosswalk_rows() if not row["clubelo_name"].strip()]
        assert blank == []

    def test_every_mapped_name_exists_in_club_elo(self) -> None:
        """Guards against a plausible-looking name that Club Elo never
        publishes ('Nott'm Forest', 'Sheffield Utd'), which would fail exactly
        as silently as a blank."""
        published = _clubelo_top_flight_names()
        mapped = {
            row["clubelo_name"].strip() for row in _crosswalk_rows() if row["clubelo_name"].strip()
        }
        assert mapped - published == set()

    def test_every_top_flight_club_is_mapped(self) -> None:
        """The other direction: a club Club Elo rates but we do not map is a
        club whose fixtures will silently lose their opponent's rating."""
        published = _clubelo_top_flight_names()
        mapped = {
            row["clubelo_name"].strip() for row in _crosswalk_rows() if row["clubelo_name"].strip()
        }
        assert published - mapped == set()

    def test_names_are_unique(self) -> None:
        """Two team codes sharing a Club Elo name would fan out the join and
        corrupt the fact table's row count."""
        names = [
            row["clubelo_name"].strip() for row in _crosswalk_rows() if row["clubelo_name"].strip()
        ]
        assert len(names) == len(set(names))


class TestEloIsFullyResolvedInFacts:
    """The end-to-end assertion the unit tests above exist to protect."""

    @pytest.mark.parametrize("year", range(2016, 2026))
    def test_no_fixture_is_missing_a_rating(self, year: int) -> None:
        part = paths.facts_table("team_fixture", Season(year), data_root=DATA_ROOT)
        part = part / "part.parquet"
        if not part.is_file():
            pytest.skip(f"team_fixture not built for {Season(year)}")
        frame = pl.read_parquet(part)
        assert frame["elo_rating"].null_count() == 0
        assert frame["opponent_elo_rating"].null_count() == 0
