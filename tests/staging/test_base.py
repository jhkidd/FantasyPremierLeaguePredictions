from __future__ import annotations

import polars as pl
import pytest

from fpl.sources.errors import SchemaError
from fpl.staging.base import ColumnSpec, TableSpec, decode_csv, stage_frame


def _spec(**overrides) -> TableSpec:
    defaults = dict(
        table="widgets",
        columns=(
            ColumnSpec("player_id", "element", pl.Int64),
            ColumnSpec("goals", "goals_scored", pl.Int64),
            ColumnSpec(
                "cbi",
                "clearances_blocks_interceptions",
                pl.Int64,
                required=False,
                group="defensive",
            ),
        ),
        key=("player_id",),
    )
    defaults.update(overrides)
    return TableSpec(**defaults)


class TestTableSpec:
    def test_rejects_duplicate_output_names(self):
        with pytest.raises(ValueError, match="duplicate"):
            TableSpec(
                table="bad",
                columns=(
                    ColumnSpec("x", "a", pl.Int64),
                    ColumnSpec("x", "b", pl.Int64),
                ),
                key=("x",),
            )

    def test_rejects_undeclared_key_column(self):
        with pytest.raises(ValueError, match="key column"):
            TableSpec(
                table="bad",
                columns=(ColumnSpec("x", "a", pl.Int64),),
                key=("y",),
            )


class TestStageFrame:
    def test_renames_and_casts(self):
        raw = pl.DataFrame({"element": ["1"], "goals_scored": ["3"]})
        staged, report = stage_frame(raw, _spec())
        assert staged.to_dicts() == [{"player_id": 1, "goals": 3, "cbi": None}]
        assert report.rows_in == 1
        assert report.rows_out == 1

    def test_unknown_column_is_a_warning_not_a_failure(self):
        raw = pl.DataFrame({"element": [1], "goals_scored": [0], "mystery": ["??"]})
        staged, report = stage_frame(raw, _spec())
        assert "mystery" not in staged.columns
        assert report.unknown_columns == ("mystery",)

    def test_missing_required_column_raises(self):
        raw = pl.DataFrame({"element": [1]})
        with pytest.raises(SchemaError, match="goals_scored"):
            stage_frame(raw, _spec())

    def test_missing_optional_column_is_typed_null(self):
        raw = pl.DataFrame({"element": [1], "goals_scored": [2]})
        staged, _ = stage_frame(raw, _spec())
        assert staged["cbi"].to_list() == [None]
        assert staged["cbi"].dtype == pl.Int64

    def test_drop_list_removes_a_column_even_when_present(self):
        raw = pl.DataFrame({"element": [1], "goals_scored": [2], "form": ["4.5"]})
        staged, report = stage_frame(raw, _spec(drop=frozenset({"form"})))
        assert "form" not in staged.columns
        assert "form" not in report.unknown_columns


class TestDecodeCsv:
    def test_decodes_cp1252_accented_names(self):
        body = "name,value\nBj\u00f6rk,1\n".encode("cp1252")
        frame = decode_csv(body, "cp1252")
        assert frame["name"].to_list() == ["Bj\u00f6rk"]

    def test_utf8_bytes_round_trip(self):
        body = b"name,value\nReinildo,7\n"
        frame = decode_csv(body, "utf-8")
        assert frame["name"].to_list() == ["Reinildo"]

    def test_declared_encoding_mismatch_raises(self):
        body = "name\nBj\u00f6rk\n".encode("cp1252")
        with pytest.raises(UnicodeDecodeError):
            decode_csv(body, "utf-8")
