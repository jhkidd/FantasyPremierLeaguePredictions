from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl.config import Season
from fpl.storage import paths
from fpl.storage.raw_io import RawArtifact, read_raw, write_raw

SEASON = Season(2026)
MOMENT = datetime(2026, 8, 1, 3, 30, 0, tzinfo=UTC)


def artifact(
    body: bytes = b'{"elements": []}',
    *,
    fetched_at: datetime = MOMENT,
    endpoint: str = "bootstrap_static",
    event: int | None = None,
    content_type: str = "json",
) -> RawArtifact:
    return RawArtifact(
        source="fpl",
        endpoint=endpoint,
        season=SEASON,
        url="https://fantasy.premierleague.com/api/bootstrap-static/",
        http_status=200,
        body=body,
        fetched_at=fetched_at,
        connector_version="1",
        event=event,
        content_type=content_type,
    )


class TestWriting:
    def test_writes_body_and_metadata(self) -> None:
        result = write_raw(artifact())
        assert result.written
        assert (result.path / "data.json.gz").is_file()
        assert (result.path / "meta.json").is_file()

    def test_body_is_gzipped(self) -> None:
        result = write_raw(artifact(b'{"elements": []}'))
        stored = (result.path / "data.json.gz").read_bytes()
        assert stored[:2] == b"\x1f\x8b"
        assert gzip.decompress(stored) == b'{"elements": []}'

    def test_metadata_records_provenance(self) -> None:
        result = write_raw(artifact())
        meta = json.loads((result.path / "meta.json").read_text(encoding="utf-8"))
        assert meta["source"] == "fpl"
        assert meta["endpoint"] == "bootstrap_static"
        assert meta["season"] == "2026-27"
        assert meta["http_status"] == 200
        assert meta["url"].startswith("https://")
        assert meta["connector_version"] == "1"
        assert meta["fetched_at"] == "2026-08-01T03:30:00+00:00"
        assert len(meta["sha256"]) == 64

    def test_content_type_selects_the_filename(self) -> None:
        result = write_raw(artifact(b'{"a": 1}\n{"a": 2}\n', content_type="ndjson"))
        assert (result.path / "data.ndjson.gz").is_file()

    def test_rejects_naive_fetched_at(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            artifact(fetched_at=datetime(2026, 8, 1, 3, 30))


class TestContentAddressing:
    def test_unchanged_body_is_not_rewritten(self) -> None:
        first = write_raw(artifact())
        second = write_raw(artifact(fetched_at=MOMENT + timedelta(days=1)))

        assert not second.written
        assert second.reason == "unchanged"
        assert second.path == first.path

    def test_unchanged_body_leaves_the_original_bytes_untouched(self) -> None:
        first = write_raw(artifact())
        before = (first.path / "meta.json").read_bytes()
        write_raw(artifact(fetched_at=MOMENT + timedelta(days=1)))
        assert (first.path / "meta.json").read_bytes() == before

    def test_unchanged_body_creates_no_new_partition(self) -> None:
        write_raw(artifact())
        write_raw(artifact(fetched_at=MOMENT + timedelta(days=1)))
        parent = paths.raw_endpoint_dir("fpl", "bootstrap_static", SEASON)
        assert len(list(parent.iterdir())) == 1

    def test_changed_body_writes_a_new_partition(self) -> None:
        write_raw(artifact(b'{"a": 1}'))
        second = write_raw(artifact(b'{"a": 2}', fetched_at=MOMENT + timedelta(days=1)))

        assert second.written
        parent = paths.raw_endpoint_dir("fpl", "bootstrap_static", SEASON)
        assert len(list(parent.iterdir())) == 2

    def test_force_writes_even_when_unchanged(self) -> None:
        write_raw(artifact())
        result = write_raw(artifact(fetched_at=MOMENT + timedelta(days=1)), force=True)
        assert result.written
        assert result.reason == "forced"

    def test_compares_against_the_latest_not_any_partition(self) -> None:
        """Content addressing looks only at the most recent capture, so a value
        that changes and later changes back is still recorded as a change."""
        write_raw(artifact(b'{"a": 1}'))
        write_raw(artifact(b'{"a": 2}', fetched_at=MOMENT + timedelta(days=1)))
        third = write_raw(artifact(b'{"a": 1}', fetched_at=MOMENT + timedelta(days=2)))
        assert third.written

    def test_corrupt_metadata_forces_a_rewrite(self) -> None:
        """Failing towards re-capturing is the safe direction."""
        first = write_raw(artifact())
        (first.path / "meta.json").write_text("not json{", encoding="utf-8")
        second = write_raw(artifact(fetched_at=MOMENT + timedelta(days=1)))
        assert second.written

    def test_events_are_addressed_independently(self) -> None:
        one = write_raw(artifact(b"same", endpoint="event_live", event=1))
        two = write_raw(artifact(b"same", endpoint="event_live", event=2))
        assert one.written and two.written
        assert one.path != two.path


class TestDeterminism:
    def test_identical_input_produces_identical_bytes(self, tmp_path: Path) -> None:
        """gzip embeds an mtime by default; pinning it is what stops every pull
        looking like a change to Git even when nothing changed."""
        first = write_raw(artifact(), data_root=tmp_path / "a")
        second = write_raw(artifact(), data_root=tmp_path / "b")

        assert (first.path / "data.json.gz").read_bytes() == (
            second.path / "data.json.gz"
        ).read_bytes()

    def test_metadata_is_byte_stable(self, tmp_path: Path) -> None:
        first = write_raw(artifact(), data_root=tmp_path / "a")
        second = write_raw(artifact(), data_root=tmp_path / "b")
        assert (first.path / "meta.json").read_bytes() == (second.path / "meta.json").read_bytes()

    def test_gzip_header_carries_no_timestamp(self) -> None:
        result = write_raw(artifact())
        header = (result.path / "data.json.gz").read_bytes()[:8]
        assert header[4:8] == b"\x00\x00\x00\x00", "mtime field should be zeroed"


class TestReading:
    def test_round_trips_body_and_metadata(self) -> None:
        body = b'{"elements": [{"id": 1}]}'
        result = write_raw(artifact(body))
        read_body, meta = read_raw(result.path)
        assert read_body == body
        assert meta["endpoint"] == "bootstrap_static"

    def test_round_trips_ndjson(self) -> None:
        body = b'{"a": 1}\n{"a": 2}\n'
        result = write_raw(artifact(body, content_type="ndjson"))
        assert read_raw(result.path)[0] == body

    def test_missing_metadata_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_raw(tmp_path)

    def test_detects_corrupted_body(self) -> None:
        """The checksum is not decoration: silent bit-rot in the raw layer
        would propagate into every model trained afterwards."""
        result = write_raw(artifact())
        (result.path / "data.json.gz").write_bytes(gzip.compress(b'{"tampered": true}', mtime=0))
        with pytest.raises(ValueError, match="checksum mismatch"):
            read_raw(result.path)
