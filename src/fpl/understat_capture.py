"""Resumable per-match Understat capture (plan §7.11).

One ``getLeagueData`` call names every fixture in a season (~380 for the
EPL); getting per-player detail needs one further ``getMatchData`` call
*per match*. At the ``understat`` source's 2s politeness interval that is
over two hours per season, ~20 hours for a full ten-season backfill -
mirroring the scale problem :mod:`fpl.ownership` already solves for entry
picks, so this reuses that module's chunked, resumable shape: batch several
matches into one :func:`fpl.storage.raw_io.write_chunk` call, and resume by
asking ``paths.iter_chunks`` which chunk indices already exist on disk
rather than tracking progress in any separate, mutable state file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fpl.config import Season
from fpl.log import event as log_event
from fpl.log import get_logger
from fpl.sources.understat import UnderstatConnector
from fpl.staging.understat import stage_fixtures
from fpl.storage import paths
from fpl.storage.raw_io import read_raw, write_chunk, write_raw

__all__ = ["CHUNK_SIZE", "CaptureOutcome", "capture_league_data", "capture_match_data"]

CHUNK_SIZE = 20
"""Matches per chunk. At ~2s/request that's ~40s of work lost to an
interrupted chunk at worst - small compared to a ~380-match season, without
the per-match partition overhead one-chunk-per-match would incur."""


@dataclass(frozen=True)
class CaptureOutcome:
    matches: int
    chunks_written: int
    chunks_skipped: int
    complete: bool

    @property
    def nothing_to_do(self) -> bool:
        return self.matches == 0


def capture_league_data(
    season: Season,
    *,
    connector: UnderstatConnector | None = None,
    data_root: Path | None = None,
    force: bool = False,
):
    """Fetch and store one season's ``getLeagueData`` payload.

    Cheap (one request), so this always uses :func:`write_raw`'s
    content-addressed skip-if-unchanged behaviour rather than chunking -
    unlike the per-match capture below, there is nothing here to resume.
    """
    owns_connector = connector is None
    connector = connector or UnderstatConnector()
    try:
        body = connector.fetch_league_data(season)
        artifact = connector.artifact_for_league_data(body, season)
    finally:
        if owns_connector:
            connector.close()
    return write_raw(artifact, force=force, data_root=data_root)


def _match_ids_for_season(season: Season, *, data_root: Path | None) -> list[int]:
    """Every played fixture's Understat match id, read from the season's
    already-captured ``getLeagueData`` response.

    Only *results* are worth an expensive ``getMatchData`` call - a fixture
    not yet played has no roster to fetch."""
    partition = paths.latest_partition("understat", "league_data", season, data_root=data_root)
    if partition is None:
        return []
    body, _meta = read_raw(partition)
    staged = stage_fixtures(body, season)
    played = staged.frame.filter(staged.frame["is_result"])
    return sorted(played["match_id"].drop_nulls().unique().to_list())


def capture_match_data(
    season: Season,
    *,
    connector: UnderstatConnector | None = None,
    data_root: Path | None = None,
    limit: int | None = None,
) -> CaptureOutcome:
    """Capture every played match's per-player roster for one season,
    resuming any partial run.

    Requires ``getLeagueData`` to already be captured for this season (via
    :func:`capture_league_data`) - that is where the match id list comes
    from, mirroring how :func:`fpl.ownership.capture_ownership` needs a
    league's standings paged before it can capture individual entries.
    """
    owns_connector = connector is None
    connector = connector or UnderstatConnector()

    try:
        match_ids = _match_ids_for_season(season, data_root=data_root)
        if limit is not None:
            match_ids = match_ids[:limit]
        if not match_ids:
            log_event(get_logger(__name__), "no_matches", season=str(season))
            return CaptureOutcome(0, 0, 0, complete=True)

        existing = {
            index
            for index, _ in paths.iter_chunks(
                "understat", "match_data", season, data_root=data_root
            )
        }

        written = skipped = 0
        for index, batch in enumerate(_batched(match_ids, CHUNK_SIZE)):
            if index in existing:
                skipped += 1
                continue
            result = _capture_chunk(connector, season, index, batch, data_root=data_root)
            written += 1 if result.written else 0
            log_event(
                get_logger(__name__),
                "chunk_captured",
                season=str(season),
                chunk=index,
                matches=len(batch),
                path=str(result.path),
            )
    finally:
        if owns_connector:
            connector.close()

    return CaptureOutcome(
        matches=len(match_ids), chunks_written=written, chunks_skipped=skipped, complete=True
    )


def _capture_chunk(
    connector: UnderstatConnector,
    season: Season,
    index: int,
    match_ids: Sequence[int],
    *,
    data_root: Path | None,
):
    """Fetch one batch of matches, storing each as its own artifact body
    inside one chunk - newline-delimited JSON, mirroring
    :mod:`fpl.ownership`'s ``entry_picks`` chunk shape."""
    import json
    from datetime import UTC, datetime

    from fpl.storage.raw_io import RawArtifact

    lines: list[bytes] = []
    for match_id in match_ids:
        body = connector.fetch_match_data(match_id)
        record = {"match_id": match_id, "payload": json.loads(body)}
        lines.append(json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8"))

    chunk_body = b"\n".join(lines) + b"\n"
    chunk_artifact = RawArtifact(
        source="understat",
        endpoint="match_data",
        season=season,
        url=f"{connector.base_url}/getMatchData/{{id}}",
        http_status=200,
        body=chunk_body,
        fetched_at=datetime.now(UTC),
        connector_version=connector.VERSION,
        params={"match_ids": list(match_ids)},
        content_type="ndjson",
    )
    return write_chunk(
        chunk_artifact,
        index,
        extra_meta={"match_count": len(match_ids)},
        data_root=data_root,
    )


def _batched(values: Sequence[int], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]
