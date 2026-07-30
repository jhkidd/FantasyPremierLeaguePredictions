from __future__ import annotations

import os
from pathlib import Path

import pytest

from fpl.config import CURRENT_SEASON, DEFAULT_DATA_ROOT, Config, Season


class TestSeasonParsing:
    @pytest.mark.parametrize(
        ("text", "start_year"),
        [("2026-27", 2026), ("2016-17", 2016), ("1999-00", 1999), (" 2026-27 ", 2026)],
    )
    def test_parses_valid_forms(self, text: str, start_year: int) -> None:
        assert Season.parse(text).start_year == start_year

    def test_century_rollover_round_trips(self) -> None:
        assert str(Season.parse("1999-00")) == "1999-00"

    @pytest.mark.parametrize(
        "text",
        ["2026", "2026-2027", "26-27", "", "2026/27", "abcd-ef", "2026-27-28"],
    )
    def test_rejects_malformed(self, text: str) -> None:
        with pytest.raises(ValueError, match="malformed season"):
            Season.parse(text)

    @pytest.mark.parametrize("text", ["2026-28", "2026-26", "2016-18"])
    def test_rejects_inconsistent_end_year(self, text: str) -> None:
        """'2026-28' is not a season. Catching it here beats silently
        partitioning two seasons' data under one directory name."""
        with pytest.raises(ValueError, match="inconsistent season"):
            Season.parse(text)

    def test_rejects_implausible_year(self) -> None:
        with pytest.raises(ValueError, match="implausible"):
            Season(1700)

    def test_round_trips_through_string(self) -> None:
        assert Season.parse(str(CURRENT_SEASON)) == CURRENT_SEASON

    def test_seasons_order_chronologically(self) -> None:
        assert sorted([Season(2020), Season(2016), Season(2026)]) == [
            Season(2016),
            Season(2020),
            Season(2026),
        ]

    def test_end_year(self) -> None:
        assert Season(2026).end_year == 2027


class TestConfig:
    def test_reads_data_root_from_environment(self, tmp_path: Path) -> None:
        os.environ["FPL_DATA_ROOT"] = str(tmp_path / "elsewhere")
        assert Config.load().data_root == tmp_path / "elsewhere"

    def test_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FPL_DATA_ROOT", raising=False)
        assert Config.load().data_root == DEFAULT_DATA_ROOT

    def test_empty_environment_value_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FPL_DATA_ROOT", "")
        assert Config.load().data_root == DEFAULT_DATA_ROOT

    def test_reads_environment_on_every_call(self, tmp_path: Path) -> None:
        """Not cached, so tests can redirect the tree mid-run."""
        os.environ["FPL_DATA_ROOT"] = str(tmp_path / "first")
        first = Config.load().data_root
        os.environ["FPL_DATA_ROOT"] = str(tmp_path / "second")
        assert Config.load().data_root != first

    def test_user_agent_identifies_the_project(self) -> None:
        agent = Config.load().user_agent
        assert "FantasyPremierLeaguePredictions" in agent
        assert "http" in agent, "should carry a contact URL so our traffic is legible"

    def test_known_source_returns_settings(self) -> None:
        assert Config.load().source("fpl").min_request_interval >= 1.0

    def test_unknown_source_names_the_alternatives(self) -> None:
        with pytest.raises(KeyError, match="known:"):
            Config.load().source("nonesuch")

    def test_backfill_is_more_polite_than_routine(self) -> None:
        """Backfill sweeps hundreds of requests; routine jobs make two."""
        config = Config.load()
        assert (
            config.source("fpl_backfill").min_request_interval
            > config.source("fpl").min_request_interval
        )
