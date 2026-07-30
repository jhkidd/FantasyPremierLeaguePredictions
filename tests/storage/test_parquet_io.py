from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from fpl.storage.parquet_io import read_parquet, write_parquet


def frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "player_id": [3, 1, 2],
            "fixture_id": [10, 10, 11],
            "minutes": [90, 45, 0],
        }
    )


def test_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    write_parquet(frame(), path)
    assert read_parquet(path).sort("player_id").equals(frame().sort("player_id"))


def test_creates_missing_parents(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "t.parquet"
    write_parquet(frame(), path)
    assert path.is_file()


def test_identical_input_produces_identical_bytes(tmp_path: Path) -> None:
    write_parquet(frame(), tmp_path / "one.parquet")
    write_parquet(frame(), tmp_path / "two.parquet")
    assert (tmp_path / "one.parquet").read_bytes() == (tmp_path / "two.parquet").read_bytes()


def test_row_order_does_not_affect_output_when_sorted(tmp_path: Path) -> None:
    """A rebuild from an unchanged source must produce an unchanged file, even
    if rows arrived in a different order. Otherwise every rebuild is a diff."""
    shuffled = frame().sort("minutes")
    write_parquet(frame(), tmp_path / "one.parquet", sort_by=["player_id", "fixture_id"])
    write_parquet(shuffled, tmp_path / "two.parquet", sort_by=["player_id", "fixture_id"])
    assert (tmp_path / "one.parquet").read_bytes() == (tmp_path / "two.parquet").read_bytes()


def test_sort_by_orders_the_rows(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    write_parquet(frame(), path, sort_by=["player_id"])
    assert read_parquet(path)["player_id"].to_list() == [1, 2, 3]


def test_sort_by_absent_column_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="absent column"):
        write_parquet(frame(), tmp_path / "t.parquet", sort_by=["nonesuch"])


def test_preserves_caller_column_order(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    write_parquet(frame(), path)
    assert read_parquet(path).columns == ["player_id", "fixture_id", "minutes"]


def test_empty_frame_round_trips(tmp_path: Path) -> None:
    """A blank gameweek legitimately produces zero rows."""
    empty = frame().head(0)
    path = tmp_path / "t.parquet"
    write_parquet(empty, path)
    assert read_parquet(path).height == 0


def test_write_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "t.parquet"
    write_parquet(frame(), path)
    good = path.read_bytes()

    monkeypatch.setattr("os.replace", lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        write_parquet(frame().head(1), path)

    assert path.read_bytes() == good
