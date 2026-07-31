"""Stage vaastav's ``merged_gw.csv``, one schema era at a time.

Seven schema eras exist across ten seasons (Finding 5). E7 (2025/26) was
staged first, in phase 4, because phase 5's reconciliation target can only
come from vaastav. The remaining six (E1-E6) are phase 6 work.

Three eras (E1, E2, E3 - 2016/17 through 2019/20) lack both a ``position``
and a ``team`` column in ``merged_gw.csv`` itself. Both are joined in here
from that season's ``players_raw.csv`` (via ``element`` -> ``id``), which
also carries the stable cross-season ``code`` field (Finding 3) - propagated
as ``player_code`` for every era where ``players_raw`` is available, not just
the early ones, since it costs nothing extra and the identity layer (phase
6.2) wants it for every season.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from fpl.config import Season
from fpl.staging.base import ColumnSpec, StagingReport, TableSpec, decode_csv, stage_frame

__all__ = [
    "ERA_BY_SEASON",
    "MERGED_GW_SPECS",
    "StagedMergedGw",
    "era_for_season",
    "stage_merged_gw",
]

ERA_BY_SEASON: dict[Season, str] = {
    Season(2016): "E1",
    Season(2017): "E1",
    Season(2018): "E2",
    Season(2019): "E3",
    Season(2020): "E4",
    Season(2021): "E4",
    Season(2022): "E5",
    Season(2023): "E5",
    Season(2024): "E6",
    Season(2025): "E7",
}
"""Which schema era a season's ``merged_gw.csv`` belongs to (Finding 5).

Deliberately a closed map: a season absent here must raise rather than be
guessed at, because a new upstream season needs a person to classify its
schema before it can be trusted (spec plan §4.6)."""

_MANAGER_ASSET_POSITION = "AM"
"""How 2024/25's manager rows are marked in the archive (Finding 6). Inert for
eras before 2024/25, kept here because the exclusion rule belongs to the
spec, not any one era."""

_ENCODING_BY_ERA: dict[str, str] = {
    "E1": "cp1252",
    "E2": "cp1252",
    "E3": "utf-8",
    "E4": "utf-8",
    "E5": "utf-8",
    "E6": "utf-8",
    "E7": "utf-8",
}

_ELEMENT_TYPE_TO_POSITION: dict[int, str] = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
"""FPL's own numeric position code (``players_raw.element_type``), used to
derive ``position`` for the three eras that never carry it in ``merged_gw``
itself."""

_POSITION_ALIASES: dict[str, str] = {"GKP": "GK"}
"""2021/22 labels goalkeepers both ways in the same season (Finding 8).
Normalised uniformly, regardless of whether ``position`` came from
``merged_gw`` directly or was derived from ``element_type`` above (which
never produces ``GKP`` in the first place, but normalising both paths the
same way means one rule to reason about, not two)."""

# --- Shared column groups -------------------------------------------------
# Every era shares these names and meanings; only which of them exist (and
# how team/position get there) differs. Declaring them once keeps the seven
# era specs below readable as *differences*, not seven independent lists.

_CORE_STATS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("goals_scored", "goals_scored", pl.Int64),
    ColumnSpec("assists", "assists", pl.Int64),
    ColumnSpec("goals_conceded", "goals_conceded", pl.Int64),
    ColumnSpec("own_goals", "own_goals", pl.Int64),
    ColumnSpec("penalties_saved", "penalties_saved", pl.Int64),
    ColumnSpec("penalties_missed", "penalties_missed", pl.Int64),
    ColumnSpec("yellow_cards", "yellow_cards", pl.Int64),
    ColumnSpec("red_cards", "red_cards", pl.Int64),
    ColumnSpec("saves", "saves", pl.Int64),
)

_OBSERVED_OUTPUT_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("bonus_fpl", "bonus", pl.Int64),
    ColumnSpec("bps_fpl", "bps", pl.Int64),
    ColumnSpec("total_points_fpl", "total_points", pl.Int64),
)

_CONTEXT_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("player_name", "name", pl.Utf8),
    ColumnSpec("player_id", "element", pl.Int64),
    ColumnSpec("fixture_id", "fixture", pl.Int64),
    ColumnSpec("event", "GW", pl.Int64),
    ColumnSpec("round", "round", pl.Int64, required=False),
    ColumnSpec("kickoff_time", "kickoff_time", pl.Utf8),
    ColumnSpec("was_home", "was_home", pl.Boolean),
    ColumnSpec("opponent_team", "opponent_team", pl.Int64),
    ColumnSpec("minutes", "minutes", pl.Int64),
    ColumnSpec("team_h_score", "team_h_score", pl.Int64, required=False),
    ColumnSpec("team_a_score", "team_a_score", pl.Int64, required=False),
)

_DEFENSIVE_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        "clearances_blocks_interceptions",
        "clearances_blocks_interceptions",
        pl.Int64,
        required=False,
    ),
    ColumnSpec("tackles", "tackles", pl.Int64, required=False),
    ColumnSpec("recoveries", "recoveries", pl.Int64, required=False),
)

_BPS_INPUT_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("attempted_passes", "attempted_passes", pl.Int64, required=False),
    ColumnSpec("completed_passes", "completed_passes", pl.Int64, required=False),
    ColumnSpec("key_passes", "key_passes", pl.Int64, required=False),
    ColumnSpec("big_chances_created", "big_chances_created", pl.Int64, required=False),
    ColumnSpec("big_chances_missed", "big_chances_missed", pl.Int64, required=False),
    ColumnSpec("open_play_crosses", "open_play_crosses", pl.Int64, required=False),
    ColumnSpec("dribbles", "dribbles", pl.Int64, required=False),
    ColumnSpec("tackled", "tackled", pl.Int64, required=False),
    ColumnSpec("fouls", "fouls", pl.Int64, required=False),
    ColumnSpec("offside", "offside", pl.Int64, required=False),
    ColumnSpec("target_missed", "target_missed", pl.Int64, required=False),
    ColumnSpec("errors_leading_to_goal", "errors_leading_to_goal", pl.Int64, required=False),
    ColumnSpec(
        "errors_leading_to_goal_attempt",
        "errors_leading_to_goal_attempt",
        pl.Int64,
        required=False,
    ),
    ColumnSpec("penalties_conceded", "penalties_conceded", pl.Int64, required=False),
    ColumnSpec("winning_goals", "winning_goals", pl.Int64, required=False),
)

_EXPECTED_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("expected_goals", "expected_goals", pl.Float64, required=False),
    ColumnSpec("expected_assists", "expected_assists", pl.Float64, required=False),
    ColumnSpec(
        "expected_goal_involvements", "expected_goal_involvements", pl.Float64, required=False
    ),
    ColumnSpec("expected_goals_conceded", "expected_goals_conceded", pl.Float64, required=False),
)

_STARTS_COLUMN = ColumnSpec("starts", "starts", pl.Int64, required=False)

# player_code and position/team_id are injected before stage_frame runs (see
# _derive_position_and_team below) for eras that never carry them natively,
# so they are always declared as *present* output columns here - stage_frame
# will pick them up from the (possibly pre-joined) raw frame like any other
# column, filling null only where the join itself found nothing.
_PLAYER_CODE_COLUMN = ColumnSpec("player_code", "player_code", pl.Utf8, required=False)
_POSITION_FROM_MERGED_GW = ColumnSpec("position", "position", pl.Utf8)
_TEAM_NAME_FROM_MERGED_GW = ColumnSpec("team", "team", pl.Utf8)
_TEAM_ID_FROM_PLAYERS_RAW = ColumnSpec("team_id", "team_id", pl.Int64, required=False)
_POSITION_FROM_PLAYERS_RAW = ColumnSpec("position", "position", pl.Utf8, required=False)

# --- Per-source-era drop lists --------------------------------------------
# Fields the spec says never to import even when present (spec §7: ep_next,
# form, xP), plus legacy/duplicate fields each era carries that we do not
# model at all - these are declared so an unknown-column warning does not
# fire for something we have already decided, deliberately, not to keep.

_DROP_COMMON = frozenset(
    {
        "value",
        "selected",
        "transfers_in",
        "transfers_out",
        "transfers_balance",
        "creativity",
        "influence",
        "threat",
        "ict_index",
        "player_id",  # left over from the players_raw join key (right_on="player_id")
        "clean_sheets",  # derived ourselves from minutes+goals_conceded, never read from FPL's flag
    }
)
_DROP_E1_E2 = _DROP_COMMON | {
    "id",  # merged_gw's own "id" duplicates "element" in E1/E2 - never the key.
    "ea_index",
    "loaned_in",
    "loaned_out",
    "kickoff_time_formatted",
}
_DROP_E4_PLUS = _DROP_COMMON | {"xP"}
_DROP_E6_E7 = _DROP_E4_PLUS | {
    "modified",
    "mng_clean_sheets",
    "mng_draw",
    "mng_goals_scored",
    "mng_loss",
    "mng_underdog_draw",
    "mng_underdog_win",
    "mng_win",
}

_E1_E2_COLUMNS: tuple[ColumnSpec, ...] = (
    *_CONTEXT_COLUMNS,
    _POSITION_FROM_PLAYERS_RAW,
    _TEAM_ID_FROM_PLAYERS_RAW,
    _PLAYER_CODE_COLUMN,
    *_CORE_STATS_COLUMNS,
    *_OBSERVED_OUTPUT_COLUMNS,
    *_DEFENSIVE_COLUMNS,
    *_BPS_INPUT_COLUMNS,
)

_E3_COLUMNS: tuple[ColumnSpec, ...] = (
    *_CONTEXT_COLUMNS,
    _POSITION_FROM_PLAYERS_RAW,
    _TEAM_ID_FROM_PLAYERS_RAW,
    _PLAYER_CODE_COLUMN,
    *_CORE_STATS_COLUMNS,
    *_OBSERVED_OUTPUT_COLUMNS,
)

_E4_COLUMNS: tuple[ColumnSpec, ...] = (
    *_CONTEXT_COLUMNS,
    _POSITION_FROM_MERGED_GW,
    _TEAM_NAME_FROM_MERGED_GW,
    _PLAYER_CODE_COLUMN,
    *_CORE_STATS_COLUMNS,
    *_OBSERVED_OUTPUT_COLUMNS,
)

_E5_COLUMNS: tuple[ColumnSpec, ...] = (
    *_CONTEXT_COLUMNS,
    _POSITION_FROM_MERGED_GW,
    _TEAM_NAME_FROM_MERGED_GW,
    _PLAYER_CODE_COLUMN,
    _STARTS_COLUMN,
    *_CORE_STATS_COLUMNS,
    *_OBSERVED_OUTPUT_COLUMNS,
    *_EXPECTED_COLUMNS,
)

_E6_COLUMNS: tuple[ColumnSpec, ...] = _E5_COLUMNS
"""Same shape as E5; ``mng_*``/``modified`` are dropped, not kept, at staging."""

_E7_COLUMNS: tuple[ColumnSpec, ...] = (
    *_CONTEXT_COLUMNS,
    _POSITION_FROM_MERGED_GW,
    _TEAM_NAME_FROM_MERGED_GW,
    _PLAYER_CODE_COLUMN,
    _STARTS_COLUMN,
    *_CORE_STATS_COLUMNS,
    *_OBSERVED_OUTPUT_COLUMNS,
    *_EXPECTED_COLUMNS,
    ColumnSpec(
        "clearances_blocks_interceptions",
        "clearances_blocks_interceptions",
        pl.Int64,
        required=False,
    ),
    ColumnSpec("tackles", "tackles", pl.Int64, required=False),
    ColumnSpec("recoveries", "recoveries", pl.Int64, required=False),
    ColumnSpec(
        "defensive_contribution",
        "defensive_contribution",
        pl.Int64,
        required=False,
    ),
)

MERGED_GW_SPECS: dict[str, TableSpec] = {
    "E1": TableSpec(
        table="player_fixture_stats",
        key=("player_id", "fixture_id"),
        columns=_E1_E2_COLUMNS,
        encoding="cp1252",
        drop=_DROP_E1_E2,
    ),
    "E2": TableSpec(
        table="player_fixture_stats",
        key=("player_id", "fixture_id"),
        columns=_E1_E2_COLUMNS,
        encoding="cp1252",
        drop=_DROP_E1_E2,
    ),
    "E3": TableSpec(
        table="player_fixture_stats",
        key=("player_id", "fixture_id"),
        columns=_E3_COLUMNS,
        encoding="utf-8",
        drop=_DROP_COMMON,
    ),
    "E4": TableSpec(
        table="player_fixture_stats",
        key=("player_id", "fixture_id"),
        columns=_E4_COLUMNS,
        encoding="utf-8",
        drop=_DROP_E4_PLUS,
    ),
    "E5": TableSpec(
        table="player_fixture_stats",
        key=("player_id", "fixture_id"),
        columns=_E5_COLUMNS,
        encoding="utf-8",
        drop=_DROP_E4_PLUS,
    ),
    "E6": TableSpec(
        table="player_fixture_stats",
        key=("player_id", "fixture_id"),
        columns=_E6_COLUMNS,
        encoding="utf-8",
        drop=_DROP_E6_E7,
    ),
    "E7": TableSpec(
        table="player_fixture_stats",
        key=("player_id", "fixture_id"),
        columns=_E7_COLUMNS,
        encoding="utf-8",
        drop=_DROP_E6_E7,
    ),
}

_ERAS_NEEDING_PLAYERS_RAW_JOIN = frozenset({"E1", "E2", "E3"})
"""Eras whose ``merged_gw.csv`` has neither ``position`` nor ``team`` - both
must come from that season's ``players_raw.csv`` (Finding 5's 33-56 column
eras never carry them directly)."""


@dataclass(frozen=True)
class StagedMergedGw:
    frame: pl.DataFrame
    report: StagingReport
    excluded_manager_rows: int
    duplicate_rows_dropped: int = 0
    """Exact byte-for-byte duplicate CSV rows dropped before staging.

    Verified live against the real 2025/26 archive: certain players (e.g.
    element 100, "Junior Kroupi") appear with every single field identical
    across two consecutive rows for the same fixture, throughout the whole
    file. This is a genuine upstream archive defect, not a staging bug — the
    duplicate is dropped here (a safe operation, since the two rows carry
    identical data) rather than surfacing as a key-uniqueness violation
    downstream, where it would look like our own bug."""
    postponed_fixture_placeholders_dropped: int = 0
    """Rows dropped because a fixture was postponed and later replayed in a
    different gameweek (verified live in 2019/20 — the COVID suspension:
    fixture 275 carries a zero-minute placeholder row at its originally
    scheduled gameweek 29, plus the real performance row at gameweek 39,
    when it was actually played, for all 59 players involved). Only the
    later gameweek's row is kept. Every dropped row is asserted to have
    ``minutes == 0`` — a nonzero-minute clash would be a genuine conflict
    needing human review, not something safe to resolve automatically."""


def _drop_postponed_fixture_placeholders(raw: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """Collapse a postponed-then-replayed fixture's two rows into one.

    A fixture rescheduled into a later gameweek can leave the archive with
    one row per (player, fixture) *per gameweek it was ever attached to* —
    a zero-minute placeholder at the gameweek it was postponed from, and the
    real performance at the gameweek it was actually played. Keeping only
    the later gameweek is safe *because* the earlier one never carries real
    data — asserted below, not assumed.
    """
    key_cols = ["element", "fixture"]
    if not set(key_cols).issubset(raw.columns) or "GW" not in raw.columns:
        return raw, 0

    max_event = raw.group_by(key_cols).agg(pl.col("GW").max().alias("_max_event"))
    tagged = raw.join(max_event, on=key_cols, how="left")
    is_stale = tagged["GW"] != tagged["_max_event"]
    stale = tagged.filter(is_stale)
    if stale.height == 0:
        return raw, 0
    if (stale["minutes"] > 0).any():
        raise ValueError(
            "postponed-fixture dedupe would drop a row with nonzero minutes "
            f"({stale.filter(pl.col('minutes') > 0).height} such row(s)); this needs human "
            "review, not automatic resolution"
        )
    kept = tagged.filter(~is_stale).drop("_max_event")
    return kept, stale.height


def era_for_season(season: Season) -> str:
    """The schema era a season's ``merged_gw.csv`` was published in.

    Raises rather than guessing, per :data:`ERA_BY_SEASON` — a season this
    map has never seen must be classified by a person before it is staged.
    """
    try:
        return ERA_BY_SEASON[season]
    except KeyError:
        raise ValueError(
            f"no schema era classified for season {season}; "
            "classify it in fpl.staging.vaastav.ERA_BY_SEASON before staging"
        ) from None


def _players_raw_lookup(body: bytes, encoding: str) -> pl.DataFrame:
    """``players_raw.csv`` -> ``(player_id, position, team_id, player_code)``.

    ``element_type`` (1-4) is FPL's own numeric position code (mapped here to
    the same GK/DEF/MID/FWD strings later eras spell out directly); ``team``
    is already the numeric team id (no name join needed for these eras,
    unlike the name-string ``team`` column later eras carry in ``merged_gw``
    itself); ``code`` is the stable cross-season key (Finding 3).
    """
    raw = decode_csv(body, encoding)
    return raw.select(
        pl.col("id").cast(pl.Int64).alias("player_id"),
        pl.col("element_type")
        .cast(pl.Int64)
        .replace_strict(_ELEMENT_TYPE_TO_POSITION, default=None)
        .alias("position"),
        pl.col("team").cast(pl.Int64).alias("team_id"),
        pl.col("code").cast(pl.Utf8).alias("player_code"),
    )


def _derive_position_and_team(
    raw: pl.DataFrame, era: str, players_raw_body: bytes | None
) -> pl.DataFrame:
    """Join in ``position``/``team_id``/``player_code`` from ``players_raw``.

    For eras that already carry ``position``/``team`` in ``merged_gw`` itself
    (E4+), only ``player_code`` is added — the name-based team-id join stays
    in facts assembly, deliberately, so staging one source never depends on
    another source having already run (see ``facts/player_fixture.py``).
    """
    if players_raw_body is None:
        if era in _ERAS_NEEDING_PLAYERS_RAW_JOIN:
            raise ValueError(
                f"era {era} has no position/team column in merged_gw.csv and "
                "requires players_raw.csv to derive them, but none was given"
            )
        return raw

    lookup = _players_raw_lookup(players_raw_body, "utf-8")
    joined = raw.join(lookup, left_on="element", right_on="player_id", how="left")
    if era not in _ERAS_NEEDING_PLAYERS_RAW_JOIN:
        # merged_gw already has its own position/team; keep those, and only
        # bring across player_code (drop the joined position/team_id).
        joined = joined.drop(["position_right", "team_id"], strict=False)
    return joined


def _normalize_position(frame: pl.DataFrame) -> pl.DataFrame:
    """GKP -> GK (Finding 8), applied uniformly regardless of source."""
    if "position" not in frame.columns:
        return frame
    return frame.with_columns(pl.col("position").replace(_POSITION_ALIASES).alias("position"))


def stage_merged_gw(
    body: bytes, season: Season, *, players_raw_body: bytes | None = None
) -> StagedMergedGw:
    """Stage one season's ``merged_gw.csv`` into ``player_fixture_stats`` rows.

    ``players_raw_body`` is required for E1-E3 (position/team derivation) and
    optional-but-recommended for every other era (stable ``player_code``).

    Manager-asset rows (Finding 6, ``position == 'AM'``) are excluded here —
    the count is returned so a caller can assert it rather than silently
    absorb it.
    """
    era = era_for_season(season)
    spec = MERGED_GW_SPECS[era]
    encoding = _ENCODING_BY_ERA[era]

    raw = decode_csv(body, encoding)
    raw = _derive_position_and_team(raw, era, players_raw_body)
    raw = _normalize_position(raw)

    is_manager_row = (
        (pl.col("position") == _MANAGER_ASSET_POSITION)
        if "position" in raw.columns
        else pl.lit(False)
    )
    excluded = raw.filter(is_manager_row).height
    raw = raw.filter(~is_manager_row)

    rows_before_dedupe = raw.height
    raw = raw.unique(maintain_order=True)
    duplicate_rows_dropped = rows_before_dedupe - raw.height

    raw, postponed_fixture_placeholders_dropped = _drop_postponed_fixture_placeholders(raw)

    staged, report = stage_frame(raw, spec)
    staged = staged.with_columns(pl.lit(str(season)).alias("season")).select(
        ["season", *staged.columns]
    )
    return StagedMergedGw(
        frame=staged,
        report=report,
        excluded_manager_rows=excluded,
        duplicate_rows_dropped=duplicate_rows_dropped,
        postponed_fixture_placeholders_dropped=postponed_fixture_placeholders_dropped,
    )
