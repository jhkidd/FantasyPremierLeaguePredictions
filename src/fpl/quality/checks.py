"""Wires the generic quality-gate framework to the concrete staged tables.

Spec §10: `fpl check` is the boundary a CI job actually calls. Table-specific
gate sets live here so :mod:`fpl.quality.gates` stays generic and reusable by
the fact layer in phase 5.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.quality.gates import (
    Gate,
    Severity,
    Violation,
    enum_values,
    in_range,
    non_negative,
    not_null,
    run_gates,
    unique_key,
)
from fpl.storage import paths

__all__ = [
    "FACTS_TABLE_GATES",
    "STAGED_TABLE_GATES",
    "check_facts_table",
    "check_facts_tables",
    "check_staged_table",
    "check_staged_tables",
]


def _elo_within_validity_window_gate(*, severity: Severity = "block") -> Gate:
    """``as_of_date`` falls inside the rating's own ``valid_from``/``valid_to``.

    Club Elo states, per row, the span of days a rating describes. That makes
    it a self-checking source: if the date we stamped a rating with sits
    outside the window the source itself published, we have misdated it.

    This gate exists because the original bug (plan §0.5) stamped ratings with
    the *fetch* date instead of the requested one — invisible to every
    null-based check, since the column was fully populated and merely wrong.
    Every row landing months outside its own window would have caught it on
    the first run.
    """
    name = "elo_within_validity_window"

    def check(frame: pl.DataFrame) -> list[Violation]:
        required = {"as_of_date", "valid_from", "valid_to"}
        if not required.issubset(frame.columns):
            missing = sorted(required - set(frame.columns))
            return [Violation(name, f"missing column(s): {missing}", severity, 0)]
        parsed = frame.with_columns(
            pl.col("as_of_date").cast(pl.Utf8).str.to_date("%Y-%m-%d", strict=False).alias("_as_of"),
            pl.col("valid_from").cast(pl.Utf8).str.to_date("%Y-%m-%d", strict=False).alias("_from"),
            pl.col("valid_to").cast(pl.Utf8).str.to_date("%Y-%m-%d", strict=False).alias("_to"),
        )
        bad = parsed.filter(
            pl.col("_as_of").is_not_null()
            & (
                (pl.col("_from").is_not_null() & (pl.col("_as_of") < pl.col("_from")))
                | (pl.col("_to").is_not_null() & (pl.col("_as_of") > pl.col("_to")))
            )
        ).drop(["_as_of", "_from", "_to"])
        if bad.height == 0:
            return []
        return [
            Violation(
                name,
                f"{bad.height} rating(s) stamped outside their own validity window",
                severity,
                bad.height,
                tuple(bad.head(5).to_dicts()),
            )
        ]

    return Gate(name, check)


STAGED_TABLE_GATES: dict[str, list[Gate]] = {
    "players": [
        unique_key(["player_id"]),
        enum_values("element_type", [1, 2, 3, 4]),
        non_negative("now_cost"),
        non_negative("minutes"),
    ],
    "teams": [unique_key(["team_id"])],
    "events": [unique_key(["event"])],
    "fixtures": [
        unique_key(["fixture_id"]),
        in_range("minutes", minimum=0, maximum=120),
    ],
    "price_snapshots": [unique_key(["player_id", "as_of_ts"])],
    "availability_snapshots": [unique_key(["player_id", "as_of_ts"])],
    "entry_snapshots": [unique_key(["entry_id", "as_of_ts"])],
    "player_fixture_stats": [
        unique_key(["player_id", "fixture_id"]),
        in_range("minutes", minimum=0, maximum=120),
        non_negative("goals_scored"),
    ],
    "clubelo_ratings": [
        unique_key(["as_of_date", "club"]),
        _elo_within_validity_window_gate(),
    ],
}


def _no_scoreless_appearance_gate(*, severity: Severity = "block") -> Gate:
    """No row may have ``minutes == 0`` and ``total_points_fpl > 0`` (spec
    §11) — a manager-asset row that slipped past staging's exclusion, or any
    other row that scored without appearing, would break this."""
    name = "no_zero_minute_positive_points"

    def check(frame: pl.DataFrame) -> list[Violation]:
        required = {"minutes", "total_points_fpl"}
        if not required.issubset(frame.columns):
            missing = required - set(frame.columns)
            return [Violation(name, f"missing column(s): {missing}", severity, 0)]
        bad = frame.filter((pl.col("minutes") == 0) & (pl.col("total_points_fpl") > 0))
        if bad.height == 0:
            return []
        return [
            Violation(
                name,
                f"{bad.height} row(s) scored with minutes == 0",
                severity,
                bad.height,
                tuple(bad.head(5).to_dicts()),
            )
        ]

    return Gate(name, check)


def _defensive_contribution_formula_gate(*, severity: Severity = "warn") -> Gate:
    """Where observed, ``defensive_contribution`` equals its own definition
    (Finding 8): ``cbi + tackles`` for defenders, ``cbi + tackles +
    recoveries`` for midfielders/forwards. Goalkeepers are excluded — the
    finding was only verified for outfield positions. Severity is ``warn``,
    not ``block``: this checks vaastav's own arithmetic, not ours, so a
    mismatch is informative rather than something our pipeline can fix."""
    name = "defensive_contribution_formula"

    def check(frame: pl.DataFrame) -> list[Violation]:
        required = {"position", "cbi", "tackles", "recoveries", "defensive_contribution"}
        if not required.issubset(frame.columns):
            return []
        observed = frame.filter(
            pl.col("obs_defensive") if "obs_defensive" in frame.columns else pl.lit(True)
        ).filter(pl.col("defensive_contribution").is_not_null())
        defenders = observed.filter(pl.col("position") == "DEF")
        bad_def = defenders.filter(
            pl.col("defensive_contribution") != (pl.col("cbi") + pl.col("tackles"))
        )
        others = observed.filter(pl.col("position").is_in(["MID", "FWD"]))
        bad_others = others.filter(
            pl.col("defensive_contribution")
            != (pl.col("cbi") + pl.col("tackles") + pl.col("recoveries"))
        )
        bad = pl.concat([bad_def, bad_others], how="vertical_relaxed")
        if bad.height == 0:
            return []
        return [
            Violation(
                name,
                f"{bad.height} row(s) where defensive_contribution disagrees with its definition",
                severity,
                bad.height,
                tuple(bad.head(5).to_dicts()),
            )
        ]

    return Gate(name, check)


def _obs_constant_within_season_gate(*, severity: Severity = "block") -> Gate:
    """Each ``obs_*`` availability flag takes exactly one value across a
    single season's facts (spec §11) — presence of a component group is a
    property of the season's schema era, not of individual rows."""
    name = "obs_constant_within_season"
    obs_columns = ("obs_defensive", "obs_bps_inputs", "obs_expected", "obs_starts")

    def check(frame: pl.DataFrame) -> list[Violation]:
        violations: list[Violation] = []
        for column in obs_columns:
            if column not in frame.columns:
                continue
            distinct = frame.select(column).unique()
            if distinct.height > 1:
                violations.append(
                    Violation(
                        name,
                        f"{column!r} takes {distinct.height} distinct values within one season",
                        severity,
                        frame.height,
                    )
                )
        return violations

    return Gate(name, check)


def _fixture_has_two_teams_gate(*, severity: Severity = "block") -> Gate:
    """Every fixture names exactly two distinct opponents (plan §0.3).

    This is the invariant the whole ``team_id`` derivation rests on: a row's
    own team is whichever of a fixture's two teams is not that row's opponent.
    It held for all 3,800 fixtures across ten seasons, but if a future season
    ever breaks it the failure mode is *silent misattribution* rather than a
    missing value, so it is asserted at ``fpl check`` time rather than assumed.
    """
    name = "fixture_has_two_teams"

    def check(frame: pl.DataFrame) -> list[Violation]:
        required = {"fixture_id", "opponent_team_id"}
        if not required.issubset(frame.columns):
            missing = sorted(required - set(frame.columns))
            return [Violation(name, f"missing column(s): {missing}", severity, 0)]
        counts = (
            frame.filter(pl.col("opponent_team_id").is_not_null())
            .group_by("fixture_id")
            .agg(pl.col("opponent_team_id").n_unique().alias("teams"))
        )
        bad = counts.filter(pl.col("teams") != 2).sort("fixture_id")
        if bad.height == 0:
            return []
        return [
            Violation(
                name,
                f"{bad.height} fixture(s) do not have exactly two distinct opponent_team_id",
                severity,
                bad.height,
                tuple(bad.head(5).to_dicts()),
            )
        ]

    return Gate(name, check)


def _team_is_not_its_own_opponent_gate(*, severity: Severity = "block") -> Gate:
    """No row has ``team_id == opponent_team_id`` (plan §0.3).

    Nulls are ignored deliberately — ``not_null("team_id")`` owns that case,
    and two gates reporting the same defect makes a failure log harder to act
    on than one that reports it once.
    """
    name = "team_is_not_its_own_opponent"

    def check(frame: pl.DataFrame) -> list[Violation]:
        required = {"team_id", "opponent_team_id"}
        if not required.issubset(frame.columns):
            missing = sorted(required - set(frame.columns))
            return [Violation(name, f"missing column(s): {missing}", severity, 0)]
        bad = frame.filter(
            pl.col("team_id").is_not_null()
            & pl.col("opponent_team_id").is_not_null()
            & (pl.col("team_id") == pl.col("opponent_team_id"))
        )
        if bad.height == 0:
            return []
        return [
            Violation(
                name,
                f"{bad.height} row(s) where team_id equals opponent_team_id",
                severity,
                bad.height,
                tuple(bad.head(5).to_dicts()),
            )
        ]

    return Gate(name, check)


FACTS_TABLE_GATES: dict[str, list[Gate]] = {
    "player_fixture": [
        unique_key(["season", "fixture_id", "player_id"]),
        in_range("minutes", minimum=0, maximum=120),
        not_null("team_id"),
        _fixture_has_two_teams_gate(),
        _team_is_not_its_own_opponent_gate(),
        _no_scoreless_appearance_gate(),
        _defensive_contribution_formula_gate(),
        _obs_constant_within_season_gate(),
    ],
    "team_fixture": [
        unique_key(["season", "fixture_id", "team_id"]),
    ],
}


def check_staged_table(
    table: str, season: Season, *, data_root: Path | None = None
) -> list[Violation]:
    """Run the declared gates for one staged table. Empty if the table is absent."""
    gates = STAGED_TABLE_GATES.get(table)
    if not gates:
        return []
    directory = paths.staged_table(table, season, data_root=data_root)
    part = directory / "part.parquet"
    if not part.is_file():
        return []
    frame = pl.read_parquet(part)
    return run_gates(frame, gates)


def check_staged_tables(season: Season, *, data_root: Path | None = None) -> list[Violation]:
    violations: list[Violation] = []
    for table in STAGED_TABLE_GATES:
        violations.extend(check_staged_table(table, season, data_root=data_root))
    return violations


def check_facts_table(
    table: str, season: Season, *, data_root: Path | None = None
) -> list[Violation]:
    """Run the declared gates for one facts table. Empty if the table is absent."""
    gates = FACTS_TABLE_GATES.get(table)
    if not gates:
        return []
    directory = paths.facts_table(table, season, data_root=data_root)
    part = directory / "part.parquet"
    if not part.is_file():
        return []
    frame = pl.read_parquet(part)
    return run_gates(frame, gates)


def check_facts_tables(season: Season, *, data_root: Path | None = None) -> list[Violation]:
    violations: list[Violation] = []
    for table in FACTS_TABLE_GATES:
        violations.extend(check_facts_table(table, season, data_root=data_root))
    return violations
