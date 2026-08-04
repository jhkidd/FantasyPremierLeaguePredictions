"""Rolling-window feature construction over ``facts/player_fixture`` history.

Every window is computed **strictly before** the target fixture's own
kickoff (enforced by the caller passing only history rows already filtered
to ``kickoff_time < as_of``), and the fixed fixture-count windows span
season boundaries freely: "last 5 fixtures" means the player's most recent
five recorded fixtures regardless of season, since a rolling window that
reset every August would make gameweek-1 features far noisier than they
need to be.

"Season-to-date" is a *separate*, season-scoped aggregate and is never
conflated with "all of history" - the caller passes it as its own frame
(``season_to_date_history``), mirroring how ``last_season_history`` is
already its own frame. ``history`` (the season-spanning frame) is used only
for the fixed :data:`FIXTURE_WINDOWS`.

For each raw component column, this module builds both a sum and a
per-90-minutes rate over every window, rather than choosing one
aggregation up front - later lasso-style feature selection is what prunes
these down, not this module (plan §7.13 already established the same
reasoning for ``fixture_count_prior_N_days``).

Availability-masked columns (defensive/BPS-input/expected-stats groups, per
``facts/player_fixture``'s own ``obs_*`` booleans) are aggregated **only**
over fixtures where that group's mask is true - a window straddling an era
boundary silently averaging in zeros for the era that never recorded the
stat would be exactly the systematic error the mask exists to prevent. Each
such windowed feature is paired with a ``..._masked_count`` companion
column recording how many of the window's fixtures were excluded because
the mask was false, so a consumer can tell "no tackles" from "no data".
"""

from __future__ import annotations

import polars as pl

__all__ = [
    "FIXTURE_WINDOWS",
    "MASKED_COLUMN_GROUPS",
    "UNMASKED_COLUMNS",
    "build_rolling_features",
]

FIXTURE_WINDOWS: tuple[int, ...] = (3, 5, 10)
"""Trailing fixture-count windows. A fixed candidate set, not one chosen
value - see module docstring."""

# Columns that are always observed (deep tier, ~10 seasons) - windowed with
# no masking needed. Mirrors fpl.facts.player_fixture's _CORE_COLUMNS plus
# minutes/starts.
UNMASKED_COLUMNS: tuple[str, ...] = (
    "minutes",
    "starts",
    "goals_scored",
    "assists",
    "goals_conceded",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "saves",
)

# (mask column, group's own columns) - the mask says whether *any* column in
# the group was observed for that row, so it applies identically to every
# column in the group. Mirrors fpl.facts.player_fixture's own grouping
# (_DEFENSIVE_COLUMNS / _BPS_INPUT_COLUMNS / _EXPECTED_COLUMNS).
MASKED_COLUMN_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("obs_defensive", ("cbi", "tackles", "recoveries", "defensive_contribution")),
    (
        "obs_bps_inputs",
        (
            "attempted_passes",
            "completed_passes",
            "key_passes",
            "big_chances_created",
            "big_chances_missed",
            "open_play_crosses",
            "dribbles",
            "tackled",
            "fouls",
            "offside",
            "target_missed",
            "errors_leading_to_goal",
            "errors_leading_to_goal_attempt",
            "penalties_conceded",
            "winning_goals",
        ),
    ),
    (
        "obs_expected",
        (
            "expected_goals",
            "expected_assists",
            "expected_goal_involvements",
            "expected_goals_conceded",
        ),
    ),
)

_UNMASKED_LABELS: tuple[str, ...] = (*(str(w) for w in FIXTURE_WINDOWS), "season_to_date", "last_season")


def _sum_and_per90(values: list, minutes: list) -> tuple[float | None, float | None]:
    present = [v for v in values if v is not None]
    if not present:
        return None, None
    total = sum(present)
    minutes_total = sum(m for m in minutes if m is not None)
    per90 = (total / minutes_total * 90) if minutes_total else None
    return total, per90


def _tail(items: list, window: int | None) -> list:
    """``window is None`` means "take the whole sequence" (used for the
    season-scoped and last-season frames, which are pre-filtered by the
    caller rather than tailed by fixture count)."""
    return items if window is None else items[-window:] if window else []


def _unmasked_for_frame(frame: pl.DataFrame, column: str, label: str) -> dict[str, float | None]:
    total, per90 = _sum_and_per90(frame[column].to_list(), frame["minutes"].to_list())
    return {
        f"{column}_sum_last_{label}": total,
        f"{column}_per90_last_{label}": per90,
    }


def _masked_for_frame(
    frame: pl.DataFrame, mask_column: str, column: str, label: str
) -> dict[str, float | None]:
    mask = frame[mask_column].to_list()
    values = frame[column].to_list()
    minutes = frame["minutes"].to_list()
    observed_values = [v for v, observed in zip(values, mask) if observed]
    observed_minutes = [m for m, observed in zip(minutes, mask) if observed]
    masked_count = sum(1 for observed in mask if not observed)
    total, per90 = _sum_and_per90(observed_values, observed_minutes)
    return {
        f"{column}_sum_last_{label}": total,
        f"{column}_per90_last_{label}": per90,
        f"{column}_masked_count_last_{label}": masked_count,
    }


def _empty_unmasked(column: str, label: str) -> dict[str, float | None]:
    return {f"{column}_sum_last_{label}": None, f"{column}_per90_last_{label}": None}


def _empty_masked(column: str, label: str) -> dict[str, float | None]:
    return {
        f"{column}_sum_last_{label}": None,
        f"{column}_per90_last_{label}": None,
        f"{column}_masked_count_last_{label}": 0,
    }


def build_rolling_features(
    history: pl.DataFrame,
    *,
    season_to_date_history: pl.DataFrame | None = None,
    last_season_history: pl.DataFrame | None = None,
) -> dict[str, float | None]:
    """Build every rolling-window feature for one player.

    ``history`` is this player's fixture history (oldest-first, already
    filtered to strictly before ``as_of``), spanning season boundaries -
    used only for the fixed :data:`FIXTURE_WINDOWS` (last 3/5/10 fixtures).

    ``season_to_date_history`` is this player's rows for the *current*
    season only (up to but not including the target fixture), used for the
    dedicated season-to-date aggregate. Pass ``None`` or an empty frame for
    a player with no fixtures yet this season.

    ``last_season_history`` is this player's rows for the single most
    recent *complete* prior season only, used for the dedicated "last
    season" aggregate (a partial season's worth of rows is aggregated as-is
    when that is all that exists, per design).
    """
    features: dict[str, float | None] = {}

    # Fixed fixture-count windows (season-spanning `history`).
    for column in UNMASKED_COLUMNS:
        for window in FIXTURE_WINDOWS:
            label = str(window)
            if history.height == 0:
                features.update(_empty_unmasked(column, label))
            else:
                minutes_tail = _tail(history["minutes"].to_list(), window)
                values_tail = _tail(history[column].to_list(), window)
                total, per90 = _sum_and_per90(values_tail, minutes_tail)
                features[f"{column}_sum_last_{label}"] = total
                features[f"{column}_per90_last_{label}"] = per90

    for mask_column, columns in MASKED_COLUMN_GROUPS:
        for column in columns:
            for window in FIXTURE_WINDOWS:
                label = str(window)
                if history.height == 0:
                    features.update(_empty_masked(column, label))
                    continue
                n = history.height
                start = max(n - window, 0)
                windowed = history.slice(start, n - start)
                features.update(_masked_for_frame(windowed, mask_column, column, label))

    # Season-to-date aggregate: a distinct, season-scoped frame - never the
    # same as "all of history".
    has_season_to_date = season_to_date_history is not None and season_to_date_history.height > 0
    for column in UNMASKED_COLUMNS:
        if has_season_to_date:
            features.update(_unmasked_for_frame(season_to_date_history, column, "season_to_date"))
        else:
            features.update(_empty_unmasked(column, "season_to_date"))
    for mask_column, columns in MASKED_COLUMN_GROUPS:
        for column in columns:
            if has_season_to_date:
                features.update(
                    _masked_for_frame(season_to_date_history, mask_column, column, "season_to_date")
                )
            else:
                features.update(_empty_masked(column, "season_to_date"))

    # Last-complete-season aggregate: sum + per-90 rate over whatever rows
    # exist for that single season (partial-season history aggregated as-is).
    has_last_season = last_season_history is not None and last_season_history.height > 0
    for column in UNMASKED_COLUMNS:
        if has_last_season:
            features.update(_unmasked_for_frame(last_season_history, column, "last_season"))
        else:
            features.update(_empty_unmasked(column, "last_season"))
    for mask_column, columns in MASKED_COLUMN_GROUPS:
        for column in columns:
            if has_last_season:
                features.update(_masked_for_frame(last_season_history, mask_column, column, "last_season"))
            else:
                features.update(_empty_masked(column, "last_season"))

    return features
