"""Per-gameweek prediction deadlines, derived from ``facts/player_fixture``.

A gameweek's deadline is the point after which none of its fixtures'
outcomes are knowable yet: ``min(kickoff_time) - 1 hour`` over that event's
fixtures. FPL's real deadlines are usually closer to 90 minutes before
kickoff, but this module deliberately derives its own value from data we
already have rather than pulling ``events.deadline_time`` from the ``events``
staged table — the training pipeline's only requirement is "strictly before
any of this gameweek's fixtures", and a value computed from the same facts
table the caller already has open needs no extra staged table and can never
drift out of sync with it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from fpl.config import Season
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet

__all__ = ["gameweek_deadlines"]

_DEADLINE_MARGIN = timedelta(hours=1)


def gameweek_deadlines(season: Season, *, data_root: Path | None = None) -> dict[int, datetime]:
    """Return ``{event: deadline}`` for every gameweek in ``season``.

    ``deadline = min(kickoff_time over that event's fixtures) - 1 hour``.

    Raises ``FileNotFoundError`` if ``facts/player_fixture`` has not been
    built for this season yet, and ``ValueError`` if any two gameweeks'
    kickoff windows overlap — a real occurrence when a postponed fixture is
    rearranged weeks later, at which point "min kickoff per event" is no
    longer a safe way to derive a deadline and the caller must resolve it
    (e.g. by excluding the rearranged fixture) before this can proceed.
    """
    path = paths.facts_table("player_fixture", season, data_root=data_root) / "part.parquet"
    if not path.exists():
        raise FileNotFoundError(f"facts/player_fixture not built for {season}: {path}")

    frame = read_parquet(path).select("event", "kickoff_time")
    frame = frame.filter(pl.col("event").is_not_null() & pl.col("kickoff_time").is_not_null())

    windows = (
        frame.group_by("event")
        .agg(
            pl.col("kickoff_time").min().alias("_start"), pl.col("kickoff_time").max().alias("_end")
        )
        .sort("_start")
    )

    _check_no_overlaps(windows, season=season)

    return {row["event"]: row["_start"] - _DEADLINE_MARGIN for row in windows.iter_rows(named=True)}


def _check_no_overlaps(windows: pl.DataFrame, *, season: Season) -> None:
    """Sweep-line check: sorted by start, an overlap exists whenever a
    window starts before the running maximum end seen so far — not just
    before the *immediately preceding* window's end, since one early,
    long-running gameweek can engulf several later ones without any single
    adjacent pair appearing to overlap."""
    running_max_end: datetime | None = None
    previous_event: int | None = None
    for row in windows.iter_rows(named=True):
        if running_max_end is not None and row["_start"] <= running_max_end:
            raise ValueError(
                f"{season}: gameweek {row['event']} kickoff window overlaps a preceding "
                f"gameweek's (up to and including gameweek {previous_event}). A fixture was "
                "likely rearranged into another gameweek's window; deadlines cannot be derived "
                "from min(kickoff_time) per event until this is resolved."
            )
        running_max_end = (
            row["_end"] if running_max_end is None else max(running_max_end, row["_end"])
        )
        previous_event = row["event"]
