"""The reconciliation milestone (spec §11, plan §5.6): for every completed
2025/26 player-fixture, ``rules_2025_26.points(row).total ==
row.total_points_fpl`` — zero tolerance.

This runs against the real data committed under ``data/`` (staged from the
actual vaastav archive via ``fpl ingest vaastav`` and ``fpl stage vaastav``),
not a synthetic fixture — the whole point of this test is to prove the
pipeline against reality, not against data we made up ourselves. It is
skipped, rather than failed, when that data has not been staged yet (e.g. a
fresh checkout before anyone has run the ingest), since an absent input is a
different problem than a wrong one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fpl.config import Season
from fpl.facts.points import build_points

SEASON = Season(2025)
DATA_ROOT = Path(__file__).resolve().parents[1] / "data"
_STAGED_2025_26 = DATA_ROOT / "staged" / "player_fixture_stats" / "season=2025-26" / "part.parquet"

pytestmark = pytest.mark.skipif(
    not _STAGED_2025_26.exists(),
    reason="2025/26 not staged into data/ yet (run: fpl ingest vaastav && fpl stage vaastav)",
)


def test_2025_26_reconciles_at_zero_tolerance() -> None:
    points = build_points(SEASON, "2025-26", data_root=DATA_ROOT)
    assert points is not None

    mismatches = points.filter(points["total"] != points["total_points_fpl"])
    if mismatches.height:
        diff = (mismatches["total"] - mismatches["total_points_fpl"]).alias("diff")
        worst = mismatches.with_columns(diff.abs().alias("_abs_diff")).sort(
            "_abs_diff", descending=True
        ).drop("_abs_diff").head(10)
        pytest.fail(f"{mismatches.height}/{points.height} rows mismatched.\nWorst rows:\n{worst}")


def test_2025_26_facts_have_no_zero_minute_positive_points_row() -> None:
    """Fact-layer invariant (spec §11): a manager-asset or otherwise
    unstaged row must never reach facts with points but no minutes."""
    points = build_points(SEASON, "2025-26", data_root=DATA_ROOT)
    assert points is not None

    from fpl.facts.player_fixture import build_player_fixture_facts

    facts = build_player_fixture_facts(SEASON, data_root=DATA_ROOT)
    violations = facts.filter((facts["minutes"] == 0) & (facts["total_points_fpl"] > 0))
    assert violations.height == 0
