"""Ownership capture — the one dataset that cannot be recovered (spec §6.1).

Two cohorts, one mechanism. The elite cohort samples the top 1,000 of the
overall league as a proxy for a field of 2.3m that cannot be enumerated. The
mini-league cohort enumerates a real opponent set exactly, for a fiftieth of
the requests. They are stored separately and never pooled, because an ownership
percentage computed across both populations would describe nobody.

Capture is chunked and resumable because Actions runners are ephemeral and a
run may die 600 entries into a 1,000-entry sweep. Resume state is "which chunk
directories exist" — there is no lock file and no mutable index to fall out of
step with the data.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fpl.config import Season
from fpl.log import event as log_event
from fpl.log import get_logger
from fpl.sources.fpl_api import OVERALL_LEAGUE_ID, FplApiConnector
from fpl.storage import paths
from fpl.storage.raw_io import RawArtifact, read_raw, write_chunk, write_raw

__all__ = [
    "CHUNK_SIZE",
    "COHORTS",
    "ELITE_COHORT",
    "ELITE_FIRST_EVENT",
    "MINI_COHORT",
    "SELF_COHORT",
    "CaptureOutcome",
    "CaptureTarget",
    "League",
    "capture_ownership",
    "collect_entry_ids",
    "current_bootstrap",
    "discover_private_leagues",
    "elite_target",
    "entries_per_page",
    "load_latest_bootstrap",
    "mini_target",
    "resolve_capture_event",
    "self_target",
]

logger = get_logger(__name__)

ELITE_COHORT = "elite"
MINI_COHORT = "mini"
SELF_COHORT = "self"
COHORTS = (SELF_COHORT, MINI_COHORT, ELITE_COHORT)

CHUNK_SIZE = 100
"""Entries per chunk. Small enough that a killed run loses little, large enough
that chunk overhead stays negligible against ~1,000 entries."""

ELITE_FIRST_EVENT = 2
"""The overall league has no standings until a gameweek has been scored
(verified 2026-07-30), so there is no top 1,000 to enumerate in gameweek 1."""

STANDINGS_PAGE_SIZE = 50

PRIVATE_LEAGUE_TYPE = "x"
"""FPL marks leagues a person created as ``x`` and leagues everyone is put into
automatically — Overall, Gameweek 1, country, sponsor — as ``s``."""


@dataclass(frozen=True)
class CaptureTarget:
    """What a single capture run is trying to collect."""

    cohort: str
    league_id: int | None
    event: int
    top: int | None = None
    """Cap on entries. ``None`` means take the whole league, which is what a
    mini-league wants and what the overall league must never be given."""

    entry_ids: tuple[int, ...] = ()
    """Entries named outright, for a cohort that is not defined by a league.
    The ``self`` cohort is one known manager, so there is no table to page."""


@dataclass(frozen=True)
class CaptureOutcome:
    target: CaptureTarget
    entries: int
    chunks_written: int
    chunks_skipped: int
    contaminated: int
    complete: bool

    @property
    def nothing_to_do(self) -> bool:
        return self.entries == 0


def resolve_capture_event(
    bootstrap: dict[str, Any],
    now: datetime,
    captured: set[int],
    *,
    first_event: int = 1,
) -> int | None:
    """The gameweek that is currently open for capture, if any.

    Open means the deadline has passed and the gameweek has not finished. That
    is a window of days, not the ninety minutes between deadline and kickoff,
    because automatic substitutions are applied at the *end* of the gameweek —
    so a manager's recorded XI reflects their decision throughout (spec §6.1).

    Returns ``None`` when there is nothing to do, which is the common case: the
    job runs every 30 minutes and fires roughly once a week.
    """
    events = bootstrap.get("events")
    if not isinstance(events, list):
        return None

    open_events: list[int] = []
    for entry in events:
        if not isinstance(entry, dict):
            continue
        event_id = entry.get("id")
        deadline = entry.get("deadline_time")
        if not isinstance(event_id, int) or not isinstance(deadline, str):
            continue
        if event_id < first_event or event_id in captured:
            continue
        if entry.get("finished"):
            continue
        deadline_at = _parse_deadline(deadline)
        if deadline_at is None or deadline_at > now:
            continue
        open_events.append(event_id)

    # Around a double gameweek two events can briefly look open at once. Take
    # the earlier: it is the one closer to expiring.
    return min(open_events) if open_events else None


def _parse_deadline(text: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def entries_per_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Extract one standings page's rows and whether another page follows."""
    standings = payload.get("standings")
    if not isinstance(standings, dict):
        return [], False
    results = standings.get("results")
    if not isinstance(results, list):
        return [], False
    return [row for row in results if isinstance(row, dict)], bool(standings.get("has_next"))


def collect_entry_ids(
    connector: FplApiConnector,
    target: CaptureTarget,
    *,
    data_root: Path | None = None,
) -> list[int]:
    """Page a league's table, storing each page raw, and return its entry IDs.

    An empty league is a legitimate answer, not an error: the overall league is
    empty until a gameweek has been scored, and a mini-league is empty until
    people join it.
    """
    if target.entry_ids:
        return list(target.entry_ids)
    if target.league_id is None:
        raise ValueError(f"cohort {target.cohort!r} has neither a league nor explicit entries")

    entry_ids: list[int] = []
    page = 1
    while True:
        artifact = connector.classic_league_standings(target.league_id, page)
        artifact = _with_cohort(artifact, target.cohort, event=target.event)
        write_raw(artifact, data_root=data_root)

        rows, has_next = entries_per_page(json.loads(artifact.body))
        entry_ids.extend(row["entry"] for row in rows if isinstance(row.get("entry"), int))

        if target.top is not None and len(entry_ids) >= target.top:
            return entry_ids[: target.top]
        if not has_next or not rows:
            break
        page += 1

        # A league with an unexpected number of pages should not spin forever.
        if page > _max_pages(target.top):
            log_event(logger, "standings_page_cap_reached", league=target.league_id, page=page)
            break

    return entry_ids[: target.top] if target.top is not None else entry_ids


def _max_pages(top: int | None) -> int:
    if top is None:
        return 200
    return max(1, -(-top // STANDINGS_PAGE_SIZE)) + 1


def capture_ownership(
    season: Season,
    target: CaptureTarget,
    *,
    connector: FplApiConnector | None = None,
    data_root: Path | None = None,
    limit: int | None = None,
) -> CaptureOutcome:
    """Capture one cohort's picks for one gameweek, resuming any partial run."""
    owns_connector = connector is None
    connector = connector or FplApiConnector(season)

    try:
        entry_ids = collect_entry_ids(connector, target, data_root=data_root)
        if limit is not None:
            entry_ids = entry_ids[:limit]
        if not entry_ids:
            log_event(
                logger,
                "no_entries",
                cohort=target.cohort,
                league=target.league_id,
                event=target.event,
            )
            return CaptureOutcome(target, 0, 0, 0, 0, complete=True)

        existing = {
            index
            for index, _ in paths.iter_chunks(
                "fpl",
                "entry_picks",
                season,
                cohort=target.cohort,
                event=target.event,
                data_root=data_root,
            )
        }

        written = skipped = contaminated = 0
        for index, batch in enumerate(_batched(entry_ids, CHUNK_SIZE)):
            if index in existing:
                skipped += 1
                continue
            result, flagged = _capture_chunk(
                connector, season, target, index, batch, data_root=data_root
            )
            written += 1 if result.written else 0
            contaminated += flagged
            log_event(
                logger,
                "chunk_captured",
                cohort=target.cohort,
                event=target.event,
                chunk=index,
                entries=len(batch),
                contaminated=flagged,
                path=result.path,
            )
    finally:
        if owns_connector:
            connector.close()

    return CaptureOutcome(
        target,
        entries=len(entry_ids),
        chunks_written=written,
        chunks_skipped=skipped,
        contaminated=contaminated,
        complete=True,
    )


def _capture_chunk(
    connector: FplApiConnector,
    season: Season,
    target: CaptureTarget,
    index: int,
    entry_ids: Sequence[int],
    *,
    data_root: Path | None,
):
    """Fetch one batch of managers into a single newline-delimited artifact."""
    lines: list[bytes] = []
    contaminated = 0
    for entry_id in entry_ids:
        artifact = connector.entry_picks(entry_id, target.event)
        payload = json.loads(artifact.body)
        is_contaminated = bool(payload.get("automatic_subs"))
        contaminated += int(is_contaminated)
        record = {
            "entry": entry_id,
            "event": target.event,
            "fetched_at": artifact.fetched_at.astimezone(UTC).isoformat(),
            "contaminated": is_contaminated,
            "payload": payload,
        }
        lines.append(json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8"))

    body = b"\n".join(lines) + b"\n"
    chunk_artifact = RawArtifact(
        source="fpl",
        endpoint="entry_picks",
        season=season,
        url=f"{connector.base_url}/entry/{{id}}/event/{target.event}/picks/",
        http_status=200,
        body=body,
        fetched_at=datetime.now(UTC),
        connector_version=connector.VERSION,
        params={"league_id": target.league_id, "entries": list(entry_ids)},
        event=target.event,
        cohort=target.cohort,
        content_type="ndjson",
    )
    result = write_chunk(
        chunk_artifact,
        index,
        extra_meta={"contaminated_entries": contaminated, "entry_count": len(entry_ids)},
        data_root=data_root,
    )
    return result, contaminated


def _with_cohort(artifact: RawArtifact, cohort: str, *, event: int) -> RawArtifact:
    """Standings pages belong to the cohort they were collected for."""
    return replace(artifact, cohort=cohort, event=event)


def _batched(values: Sequence[int], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def load_latest_bootstrap(
    season: Season, *, data_root: Path | None = None
) -> dict[str, Any] | None:
    """Read the most recent stored `bootstrap-static`, or None if there is none."""
    partition = paths.latest_partition("fpl", "bootstrap_static", season, data_root=data_root)
    if partition is None:
        return None
    body, _meta = read_raw(partition)
    payload = json.loads(body)
    return payload if isinstance(payload, dict) else None


def current_bootstrap(
    season: Season,
    *,
    connector: FplApiConnector | None = None,
    data_root: Path | None = None,
) -> dict[str, Any] | None:
    """The freshest `bootstrap-static` available, fetched but deliberately not stored.

    Capture needs the live `finished` flags to tell an open gameweek from a
    closed one, but it runs every 30 minutes and bootstrap changes constantly
    during a season. Persisting it here would commit a 117 KB snapshot 48 times
    a day and duplicate the job the daily snapshot exists to do, so this holds
    the payload in memory instead.

    Falls back to the stored copy when the API is unreachable. That copy is at
    most a day old, and deadlines — the part that decides whether to capture —
    do not move.
    """
    owns = connector is None
    client = connector or FplApiConnector(season)
    try:
        artifact = client.bootstrap_static()
    except Exception as exc:  # noqa: BLE001 - any fetch failure falls back to disk
        log_event(logger, "bootstrap_live_read_failed", error=str(exc))
        return load_latest_bootstrap(season, data_root=data_root)
    finally:
        if owns:
            client.close()

    payload = json.loads(artifact.body)
    return payload if isinstance(payload, dict) else None


def elite_target(event: int, top: int) -> CaptureTarget:
    return CaptureTarget(ELITE_COHORT, OVERALL_LEAGUE_ID, event, top=top)


def mini_target(league_id: int, event: int) -> CaptureTarget:
    return CaptureTarget(MINI_COHORT, league_id, event, top=None)


def self_target(entry_id: int, event: int) -> CaptureTarget:
    """Our own squad.

    Captured for the same reason as everyone else's: FPL discards it at season
    rollover. Without it there is no record of what we actually played, and so
    no way to measure a recommendation against the decision that was taken —
    which is the whole of evaluation (spec §2).
    """
    return CaptureTarget(SELF_COHORT, None, event, top=None, entry_ids=(entry_id,))


@dataclass(frozen=True)
class League:
    id: int
    name: str


def discover_private_leagues(
    connector: FplApiConnector, entry_id: int, *, data_root: Path | None = None
) -> list[League]:
    """The leagues this manager joined deliberately, newest first.

    FPL puts everyone into Overall, Gameweek 1, a country league and whatever
    sponsor league is running; those are marked ``s`` and are not opponents in
    any meaningful sense. Only ``x`` leagues were created by a person.

    Returns them so the caller can decide. Picking one is a judgement — this
    function does not make it.
    """
    artifact = connector.entry(entry_id)
    write_raw(artifact, data_root=data_root)
    payload = json.loads(artifact.body)

    leagues = payload.get("leagues")
    classic = leagues.get("classic") if isinstance(leagues, dict) else None
    if not isinstance(classic, list):
        return []

    found: list[League] = []
    for row in classic:
        if not isinstance(row, dict) or row.get("league_type") != PRIVATE_LEAGUE_TYPE:
            continue
        league_id, name = row.get("id"), row.get("name")
        if isinstance(league_id, int) and isinstance(name, str):
            found.append(League(league_id, name))
    return found
