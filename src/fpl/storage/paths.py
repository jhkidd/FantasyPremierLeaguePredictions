"""Path and partition conventions for the data tree.

This is the **only** module that knows where anything lives. Everything else
asks it for a path. That containment is what makes the escape hatch in spec §12
— moving from Git to object storage — a change to one module rather than a
change everywhere.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from fpl.config import Config, Season

__all__ = [
    "chunk_partition",
    "decode_as_of",
    "encode_as_of",
    "facts_table",
    "iter_chunks",
    "latest_partition",
    "raw_endpoint_dir",
    "raw_partition",
    "staged_table",
]

# Colons are illegal in Windows filenames, and development happens on Windows
# while execution happens on Linux. Hyphens keep the format lexicographically
# sortable, which `latest_partition` depends on.
AS_OF_FORMAT: Final = "%Y-%m-%dT%H-%M-%SZ"

_FORBIDDEN_IN_COMPONENT: Final = frozenset('<>:"/\\|?*')


def encode_as_of(moment: datetime) -> str:
    """Render a timestamp as a path-safe, sortable partition value."""
    if moment.tzinfo is None:
        raise ValueError("as_of must be timezone-aware; naive datetimes are ambiguous")
    return moment.astimezone(UTC).strftime(AS_OF_FORMAT)


def decode_as_of(text: str) -> datetime:
    """Inverse of :func:`encode_as_of`. Returns a UTC-aware datetime."""
    return datetime.strptime(text, AS_OF_FORMAT).replace(tzinfo=UTC)


def _check_component(value: str, *, label: str) -> str:
    """Reject anything that would escape or corrupt the data tree."""
    if not value:
        raise ValueError(f"{label} must not be empty")
    bad = _FORBIDDEN_IN_COMPONENT.intersection(value)
    if bad:
        raise ValueError(f"{label} {value!r} contains illegal path characters: {sorted(bad)}")
    if value in {".", ".."}:
        raise ValueError(f"{label} must not be a relative path segment")
    return value


def _normalise_endpoint(endpoint: str) -> str:
    """`bootstrap-static` and `bootstrap_static` name the same partition."""
    return _check_component(endpoint.replace("-", "_"), label="endpoint")


def _root(data_root: Path | None) -> Path:
    return data_root if data_root is not None else Config.load().data_root


def raw_endpoint_dir(
    source: str,
    endpoint: str,
    season: Season,
    *,
    event: int | None = None,
    data_root: Path | None = None,
) -> Path:
    """The directory holding every capture of one endpoint, for one season."""
    path = (
        _root(data_root)
        / "raw"
        / _check_component(source, label="source")
        / _normalise_endpoint(endpoint)
        / f"season={season}"
    )
    if event is not None:
        path = path / f"event={int(event)}"
    return path


def raw_partition(
    source: str,
    endpoint: str,
    season: Season,
    as_of: datetime,
    *,
    event: int | None = None,
    data_root: Path | None = None,
) -> Path:
    """The directory for a single capture at a single moment."""
    parent = raw_endpoint_dir(source, endpoint, season, event=event, data_root=data_root)
    return parent / f"as_of={encode_as_of(as_of)}"


def chunk_partition(
    source: str,
    endpoint: str,
    season: Season,
    chunk: int,
    *,
    event: int | None = None,
    data_root: Path | None = None,
) -> Path:
    """The directory for one chunk of a resumable capture.

    Used by ownership capture (spec §6.1), where a run may be interrupted and
    resumed. Chunks are numbered rather than timestamped because the chunk
    index *is* the resume state — "which chunks exist" is the whole protocol.
    Each chunk records its own capture time in its metadata.
    """
    if chunk < 0:
        raise ValueError(f"chunk index must not be negative: {chunk}")
    parent = raw_endpoint_dir(source, endpoint, season, event=event, data_root=data_root)
    return parent / f"chunk={chunk:04d}"


def latest_partition(
    source: str,
    endpoint: str,
    season: Season,
    *,
    event: int | None = None,
    data_root: Path | None = None,
) -> Path | None:
    """The most recent capture of an endpoint, or None if there is none.

    Found by listing sibling directories rather than by consulting an index, so
    the raw tree stays purely append-only — there is no mutable file to fall out
    of step with reality.
    """
    parent = raw_endpoint_dir(source, endpoint, season, event=event, data_root=data_root)
    if not parent.is_dir():
        return None
    partitions = [p for p in parent.iterdir() if p.is_dir() and p.name.startswith("as_of=")]
    return max(partitions, key=lambda p: p.name) if partitions else None


def iter_chunks(
    source: str,
    endpoint: str,
    season: Season,
    *,
    event: int | None = None,
    data_root: Path | None = None,
) -> Iterator[tuple[int, Path]]:
    """Yield ``(index, path)`` for every chunk already captured, in order."""
    parent = raw_endpoint_dir(source, endpoint, season, event=event, data_root=data_root)
    if not parent.is_dir():
        return
    chunks = [p for p in parent.iterdir() if p.is_dir() and p.name.startswith("chunk=")]
    for path in sorted(chunks, key=lambda p: p.name):
        yield int(path.name.removeprefix("chunk=")), path


def staged_table(name: str, season: Season, *, data_root: Path | None = None) -> Path:
    return _root(data_root) / "staged" / _check_component(name, label="table") / f"season={season}"


def facts_table(
    name: str,
    season: Season,
    *,
    rules: str | None = None,
    data_root: Path | None = None,
) -> Path:
    """Canonical facts. ``rules`` partitions points derived under a ruleset.

    Spec §4: points are derived from component stats under a versioned ruleset
    rather than ingested, so the same season can be scored several ways and each
    scoring lives side by side.
    """
    path = _root(data_root) / "facts" / _check_component(name, label="table")
    if rules is not None:
        path = path / f"rules={_check_component(rules, label='rules')}"
    return path / f"season={season}"
