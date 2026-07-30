from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from fpl.storage.atomic import atomic_write_bytes


def test_writes_data(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    atomic_write_bytes(target, b"hello")
    assert target.read_bytes() == b"hello"


def test_creates_missing_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c" / "file.bin"
    atomic_write_bytes(target, b"hello")
    assert target.read_bytes() == b"hello"


def test_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    atomic_write_bytes(tmp_path / "file.bin", b"hello")
    assert [p.name for p in tmp_path.iterdir()] == ["file.bin"]


def test_overwrites_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    atomic_write_bytes(target, b"first")
    atomic_write_bytes(target, b"second")
    assert target.read_bytes() == b"second"


def test_failure_mid_write_leaves_no_partial_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure this whole module exists to prevent: a truncated file that
    looks complete, which everything downstream then trusts."""
    target = tmp_path / "file.bin"
    real_open = builtins.open

    class ExplodingHandle:
        def __init__(self, inner):
            self._inner = inner

        def write(self, _data):
            raise OSError("disk full")

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    def exploding_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        return ExplodingHandle(handle) if "w" in mode else handle

    monkeypatch.setattr(builtins, "open", exploding_open)

    with pytest.raises(OSError, match="disk full"):
        atomic_write_bytes(target, b"payload")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == [], "a temp file was left behind"


def test_failure_leaves_previous_version_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed rewrite must degrade to 'yesterday's data', never to 'no data'."""
    target = tmp_path / "file.bin"
    atomic_write_bytes(target, b"good")

    def exploding_replace(*_args, **_kwargs):
        raise OSError("rename failed")

    monkeypatch.setattr("os.replace", exploding_replace)
    with pytest.raises(OSError, match="rename failed"):
        atomic_write_bytes(target, b"bad")

    assert target.read_bytes() == b"good"
    assert [p.name for p in tmp_path.iterdir()] == ["file.bin"]


def test_temp_file_is_a_sibling_of_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-filesystem renames are not atomic, so the temp file must share a
    directory with its target rather than live in the system temp dir."""
    target = tmp_path / "nested" / "file.bin"
    seen: list[Path] = []

    real_replace = __import__("os").replace

    def recording_replace(src, dst):
        seen.append(Path(src))
        return real_replace(src, dst)

    monkeypatch.setattr("os.replace", recording_replace)
    atomic_write_bytes(target, b"payload")

    assert seen and seen[0].parent == target.parent


def test_empty_payload_is_written(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    atomic_write_bytes(target, b"")
    assert target.exists() and target.read_bytes() == b""
