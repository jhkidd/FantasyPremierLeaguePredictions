"""Reading and writing the immutable raw layer.

Two properties keep the raw tree from bloating the repository (spec §12):

* **Content addressing** — a capture whose bytes match the previous capture is
  not written at all. Sources that genuinely did not change between pulls cost
  nothing.
* **Deterministic gzip** — the compressed stream embeds no timestamp, so
  identical input always produces identical output bytes. Without that, every
  pull would look like a change to Git even when nothing changed.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fpl.config import Season
from fpl.storage.atomic import atomic_write_bytes
from fpl.storage.paths import (
    chunk_partition,
    decode_as_of,
    encode_as_of,
    latest_partition,
    raw_partition,
)

__all__ = [
    "META_FILENAME",
    "RawArtifact",
    "WriteResult",
    "read_raw",
    "write_chunk",
    "write_raw",
]

META_FILENAME = "meta.json"

# gzip stores a modification time in its header. Pinning it to zero is what
# makes two compressions of the same bytes byte-identical.
_GZIP_MTIME = 0
_GZIP_LEVEL = 9


@dataclass(frozen=True)
class RawArtifact:
    """Exactly what one source returned for one request, plus its provenance."""

    source: str
    endpoint: str
    season: Season
    url: str
    http_status: int
    body: bytes
    fetched_at: datetime
    connector_version: str
    params: Mapping[str, Any] = field(default_factory=dict)
    event: int | None = None
    cohort: str | None = None
    """Population this capture describes, when one endpoint serves several
    that must never be pooled (spec §6.1)."""
    content_type: str = "json"
    """File extension beneath the ``.gz``. ``json`` or ``ndjson``."""

    def __post_init__(self) -> None:
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def filename(self) -> str:
        return f"data.{self.content_type}.gz"


@dataclass(frozen=True)
class WriteResult:
    path: Path
    written: bool
    reason: str


def _meta_dict(artifact: RawArtifact, *, compressed_bytes: int) -> dict[str, Any]:
    return {
        "cohort": artifact.cohort,
        "compressed_bytes": compressed_bytes,
        "connector_version": artifact.connector_version,
        "content_type": artifact.content_type,
        "endpoint": artifact.endpoint,
        "event": artifact.event,
        "fetched_at": artifact.fetched_at.astimezone(UTC).isoformat(),
        "http_status": artifact.http_status,
        "params": dict(artifact.params),
        "raw_bytes": len(artifact.body),
        "season": str(artifact.season),
        "sha256": artifact.sha256,
        "source": artifact.source,
        "url": artifact.url,
    }


def _read_meta(partition: Path) -> dict[str, Any] | None:
    meta_path = partition / META_FILENAME
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt sidecar must not silently suppress a write; treating it as
        # absent means we re-capture, which is the safe direction to fail in.
        return None


def write_raw(
    artifact: RawArtifact,
    *,
    force: bool = False,
    data_root: Path | None = None,
) -> WriteResult:
    """Persist an artifact, skipping the write if its content is unchanged.

    Returns a :class:`WriteResult` describing what happened. When the write is
    skipped, ``path`` points at the existing partition that already holds these
    bytes.

    Skipping means we do not record "at time T the source was unchanged".
    Freshness is therefore tracked by ``status.json`` and the workflow run
    history, not by the raw tree — the deliberate trade-off noted in spec §12.
    """
    if not force:
        previous = latest_partition(
            artifact.source,
            artifact.endpoint,
            artifact.season,
            cohort=artifact.cohort,
            event=artifact.event,
            data_root=data_root,
        )
        if previous is not None:
            meta = _read_meta(previous)
            if meta is not None and meta.get("sha256") == artifact.sha256:
                return WriteResult(previous, written=False, reason="unchanged")

    partition = raw_partition(
        artifact.source,
        artifact.endpoint,
        artifact.season,
        artifact.fetched_at,
        cohort=artifact.cohort,
        event=artifact.event,
        data_root=data_root,
    )
    compressed = gzip.compress(artifact.body, compresslevel=_GZIP_LEVEL, mtime=_GZIP_MTIME)
    atomic_write_bytes(partition / artifact.filename, compressed)

    meta_bytes = (
        json.dumps(
            _meta_dict(artifact, compressed_bytes=len(compressed)),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
    atomic_write_bytes(partition / META_FILENAME, meta_bytes)

    return WriteResult(partition, written=True, reason="new" if not force else "forced")


def write_chunk(
    artifact: RawArtifact,
    chunk: int,
    *,
    extra_meta: Mapping[str, Any] | None = None,
    data_root: Path | None = None,
) -> WriteResult:
    """Persist one chunk of a resumable capture.

    Unlike :func:`write_raw` there is no content addressing: a chunk is
    identified by its index, and two chunks holding identical bytes are still
    different chunks. An existing chunk is never overwritten, because its
    presence is the resume protocol (spec §6.1) — rewriting it would silently
    discard the very state the next run depends on.
    """
    partition = chunk_partition(
        artifact.source,
        artifact.endpoint,
        artifact.season,
        chunk,
        cohort=artifact.cohort,
        event=artifact.event,
        data_root=data_root,
    )
    if (partition / META_FILENAME).is_file():
        return WriteResult(partition, written=False, reason="already_captured")

    compressed = gzip.compress(artifact.body, compresslevel=_GZIP_LEVEL, mtime=_GZIP_MTIME)
    atomic_write_bytes(partition / artifact.filename, compressed)

    meta = _meta_dict(artifact, compressed_bytes=len(compressed))
    meta["chunk"] = chunk
    meta.update(extra_meta or {})
    meta_bytes = (
        json.dumps(meta, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    )
    atomic_write_bytes(partition / META_FILENAME, meta_bytes)

    return WriteResult(partition, written=True, reason="new")


def read_raw(partition: Path) -> tuple[bytes, dict[str, Any]]:
    """Return the decompressed body and metadata of a stored capture."""
    meta = _read_meta(partition)
    if meta is None:
        raise FileNotFoundError(f"no readable {META_FILENAME} in {partition}")
    body_path = partition / f"data.{meta.get('content_type', 'json')}.gz"
    body = gzip.decompress(body_path.read_bytes())

    actual = hashlib.sha256(body).hexdigest()
    if actual != meta["sha256"]:
        raise ValueError(
            f"checksum mismatch in {partition}: metadata says {meta['sha256']}, "
            f"stored bytes hash to {actual}"
        )
    return body, meta


def partition_as_of(partition: Path) -> datetime:
    """Recover the capture time encoded in a partition directory name."""
    name = partition.name
    if not name.startswith("as_of="):
        raise ValueError(f"not an as_of partition: {partition}")
    return decode_as_of(name.removeprefix("as_of="))


def as_of_name(moment: datetime) -> str:
    """Public re-export so callers need not import from two storage modules."""
    return encode_as_of(moment)
