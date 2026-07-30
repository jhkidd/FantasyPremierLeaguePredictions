"""Orchestration between connectors and storage.

Thin by design: it decides *what* to fetch and hands the result to storage. It
does not parse or transform, so a bug here can only ever cost us a fetch, never
corrupt what is already on disk.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from fpl.config import Season
from fpl.log import event as log_event
from fpl.log import get_logger
from fpl.sources.fpl_api import FplApiConnector
from fpl.storage.raw_io import WriteResult, write_raw

__all__ = ["ROUTINE_ENDPOINTS", "SUPPORTED_ENDPOINTS", "ingest_fpl"]

logger = get_logger(__name__)

ROUTINE_ENDPOINTS: tuple[str, ...] = ("bootstrap-static", "fixtures")
"""What `daily-snapshot` pulls: two requests, run after FPL's nightly price update."""

SUPPORTED_ENDPOINTS: tuple[str, ...] = (
    "bootstrap-static",
    "fixtures",
    "event-live",
    "element-summary",
)


def ingest_fpl(
    season: Season,
    endpoints: Sequence[str] | None = None,
    *,
    event: int | None = None,
    player_id: int | None = None,
    data_root: Path | None = None,
    connector: FplApiConnector | None = None,
    force: bool = False,
) -> list[WriteResult]:
    """Fetch one or more FPL endpoints into the raw layer.

    Idempotent: re-running with unchanged upstream data writes nothing, because
    storage is content-addressed.
    """
    selected = tuple(endpoints) if endpoints else ROUTINE_ENDPOINTS
    unknown = [name for name in selected if name not in SUPPORTED_ENDPOINTS]
    if unknown:
        raise ValueError(f"unknown endpoint(s) {unknown}; supported: {list(SUPPORTED_ENDPOINTS)}")

    owns_connector = connector is None
    connector = connector or FplApiConnector(season)
    results: list[WriteResult] = []

    try:
        for name in selected:
            artifact = _fetch(connector, name, event=event, player_id=player_id)
            result = write_raw(artifact, force=force, data_root=data_root)
            results.append(result)
            log_event(
                logger,
                "ingested",
                source="fpl",
                endpoint=artifact.endpoint,
                season=season,
                event=artifact.event,
                status=artifact.http_status,
                bytes=len(artifact.body),
                sha=artifact.sha256[:12],
                stored=result.reason,
                path=result.path,
            )
    finally:
        if owns_connector:
            connector.close()

    return results


def _fetch(connector: FplApiConnector, endpoint: str, *, event: int | None, player_id: int | None):
    if endpoint == "bootstrap-static":
        return connector.bootstrap_static()
    if endpoint == "fixtures":
        return connector.fixtures(event=event)
    if endpoint == "event-live":
        if event is None:
            raise ValueError("event-live requires --event")
        return connector.event_live(event)
    if endpoint == "element-summary":
        if player_id is None:
            raise ValueError("element-summary requires --player")
        return connector.element_summary(player_id)
    raise ValueError(f"unhandled endpoint {endpoint!r}")
