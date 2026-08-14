"""The reconciliation milestone (spec §11, plan §5.6 / §6.5): for every
completed player-fixture in **all ten backfilled seasons**, the relevant
ruleset's derived total equals FPL's own published total — zero tolerance.

This runs against the real data committed under ``data/`` (staged from the
actual vaastav archive via ``fpl ingest vaastav`` and ``fpl stage vaastav``),
not a synthetic fixture — the whole point of this test is to prove the
pipeline against reality, not against data we made up ourselves. Each season
is skipped individually, rather than failed, when that season has not been
staged yet (e.g. a fresh checkout before anyone has run the backfill), since
an absent input is a different problem than a wrong one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fpl.config import Season
from fpl.facts.player_fixture import build_player_fixture_facts
from fpl.facts.points import build_points

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"

# season -> ruleset name (plan's locked decision: legacy for 2016/17-2024/25,
# a dedicated module per points-affecting change thereafter).
_SEASON_RULES: dict[Season, str] = {
    Season(2016): "legacy",
    Season(2017): "legacy",
    Season(2018): "legacy",
    Season(2019): "legacy",
    Season(2020): "legacy",
    Season(2021): "legacy",
    Season(2022): "legacy",
    Season(2023): "legacy",
    Season(2024): "legacy",
    Season(2025): "2025-26",
}


def _staged_path(season: Season) -> Path:
    return DATA_ROOT / "staged" / "player_fixture_stats" / f"season={season}" / "part.parquet"


def _staged_seasons() -> list[tuple[Season, str]]:
    return [
        (season, rules) for season, rules in _SEASON_RULES.items() if _staged_path(season).exists()
    ]


_STAGED = _staged_seasons()

pytestmark = pytest.mark.skipif(
    not _STAGED,
    reason="no season staged into data/ yet (run: fpl ingest vaastav && fpl stage vaastav)",
)


@pytest.mark.parametrize("season,rules", _STAGED, ids=[str(s) for s, _ in _STAGED])
def test_reconciles_at_zero_tolerance(season: Season, rules: str) -> None:
    points = build_points(season, rules, data_root=DATA_ROOT)
    assert points is not None

    mismatches = points.filter(points["total"] != points["total_points_fpl"])
    if mismatches.height:
        diff = (mismatches["total"] - mismatches["total_points_fpl"]).alias("diff")
        worst = (
            mismatches.with_columns(diff.abs().alias("_abs_diff"))
            .sort("_abs_diff", descending=True)
            .drop("_abs_diff")
            .head(10)
        )
        pytest.fail(f"{mismatches.height}/{points.height} rows mismatched.\nWorst rows:\n{worst}")


@pytest.mark.parametrize("season,rules", _STAGED, ids=[str(s) for s, _ in _STAGED])
def test_facts_have_no_zero_minute_positive_points_row(season: Season, rules: str) -> None:
    """Fact-layer invariant (spec §11): a manager-asset or otherwise
    unstaged row must never reach facts with points but no minutes."""
    facts = build_player_fixture_facts(season, data_root=DATA_ROOT)
    assert facts is not None

    violations = facts.filter((facts["minutes"] == 0) & (facts["total_points_fpl"] > 0))
    assert violations.height == 0


@pytest.mark.parametrize("season,rules", _STAGED, ids=[str(s) for s, _ in _STAGED])
def test_key_is_unique(season: Season, rules: str) -> None:
    facts = build_player_fixture_facts(season, data_root=DATA_ROOT)
    assert facts is not None
    assert facts.select(["fixture_id", "player_id"]).is_duplicated().sum() == 0
