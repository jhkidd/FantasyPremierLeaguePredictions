"""Derive itemised points under a named ruleset from ``player_fixture`` facts
(spec §5.5, plan §5.6 — the reconciliation milestone).

Kept separate from :mod:`fpl.facts.player_fixture` so scoring stays a pure
function of one row (per :mod:`fpl.scoring.base`'s design) and this module's
only job is to iterate the facts frame and assemble the results table.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.facts.player_fixture import build_player_fixture_facts
from fpl.scoring.base import PlayerFixtureRow, Rules
from fpl.scoring.rules_2025_26 import NAME as RULES_2025_26_NAME
from fpl.scoring.rules_2025_26 import Rules202526
from fpl.scoring.rules_2026_27 import NAME as RULES_2026_27_NAME
from fpl.scoring.rules_2026_27 import Rules202627
from fpl.scoring.rules_legacy import NAME as RULES_LEGACY_NAME
from fpl.scoring.rules_legacy import LegacyRules
from fpl.storage import paths
from fpl.storage.parquet_io import write_parquet

__all__ = [
    "KEY",
    "RULESETS",
    "PointsResult",
    "build_points",
    "ruleset_for_name",
    "write_points",
]

KEY: tuple[str, ...] = ("season", "fixture_id", "player_id")

RULESETS: dict[str, Rules] = {
    RULES_LEGACY_NAME: LegacyRules(),
    RULES_2025_26_NAME: Rules202526(),
    RULES_2026_27_NAME: Rules202627(),
}


def ruleset_for_name(name: str) -> Rules:
    try:
        return RULESETS[name]
    except KeyError:
        raise ValueError(f"unknown ruleset {name!r}; known: {sorted(RULESETS)}") from None


def _row_to_player_fixture(row: dict) -> PlayerFixtureRow:
    return PlayerFixtureRow(
        position=row["position"],
        minutes=row["minutes"],
        goals_scored=row["goals_scored"],
        assists=row["assists"],
        goals_conceded=row["goals_conceded"],
        own_goals=row["own_goals"],
        penalties_saved=row["penalties_saved"],
        penalties_missed=row["penalties_missed"],
        yellow_cards=row["yellow_cards"],
        red_cards=row["red_cards"],
        saves=row["saves"],
        bonus=row["bonus_fpl"],
        cbi=row["cbi"] or 0,
        tackles=row["tackles"] or 0,
        recoveries=row["recoveries"] or 0,
    )


@dataclass(frozen=True)
class PointsResult:
    frame: pl.DataFrame | None
    written: bool
    detail: str = ""


def build_points(
    season: Season, rules_name: str, *, data_root: Path | None = None
) -> pl.DataFrame | None:
    """Score every row of one season's facts under one named ruleset.

    Returns ``None`` when there are no facts to score yet — the same
    "nothing to do" convention as :func:`build_player_fixture_facts`.
    """
    facts = build_player_fixture_facts(season, data_root=data_root)
    if facts is None:
        return None

    rules = ruleset_for_name(rules_name)
    breakdown_rows = []
    for row in facts.select(list(KEY) + list(_INPUT_COLUMNS)).iter_rows(named=True):
        breakdown = rules.points(_row_to_player_fixture(row))
        breakdown_rows.append(
            {
                "season": row["season"],
                "fixture_id": row["fixture_id"],
                "player_id": row["player_id"],
                **asdict(breakdown),
                "total": breakdown.total,
            }
        )

    points = pl.DataFrame(breakdown_rows)
    return points.join(
        facts.select([*KEY, "total_points_fpl"]),
        on=list(KEY),
        how="left",
    )


_INPUT_COLUMNS: tuple[str, ...] = (
    "position",
    "minutes",
    "goals_scored",
    "assists",
    "goals_conceded",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "saves",
    "bonus_fpl",
    "cbi",
    "tackles",
    "recoveries",
)


def write_points(
    season: Season, rules_name: str, *, data_root: Path | None = None
) -> PointsResult:
    """Build and write ``facts/points/rules=.../season=.../part.parquet``."""
    frame = build_points(season, rules_name, data_root=data_root)
    if frame is None:
        return PointsResult(None, False, "no player_fixture facts for this season")

    out_dir = paths.facts_table("points", season, rules=rules_name, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(frame, out_dir / "part.parquet", sort_by=list(KEY))
    return PointsResult(frame, True)
