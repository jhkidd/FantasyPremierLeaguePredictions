from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fpl.config import Season
from fpl.storage import paths

SEASON = Season(2026)
MOMENT = datetime(2026, 8, 1, 3, 30, 0, tzinfo=UTC)


class TestAsOfEncoding:
    def test_encodes_without_colons(self) -> None:
        """Windows forbids ':' in filenames and development happens on Windows."""
        assert ":" not in paths.encode_as_of(MOMENT)

    def test_expected_form(self) -> None:
        assert paths.encode_as_of(MOMENT) == "2026-08-01T03-30-00Z"

    def test_round_trips(self) -> None:
        assert paths.decode_as_of(paths.encode_as_of(MOMENT)) == MOMENT

    def test_converts_to_utc(self) -> None:
        local = MOMENT.astimezone(timezone(timedelta(hours=5)))
        assert paths.encode_as_of(local) == paths.encode_as_of(MOMENT)

    def test_truncates_sub_second_precision(self) -> None:
        precise = MOMENT.replace(microsecond=123456)
        assert paths.encode_as_of(precise) == paths.encode_as_of(MOMENT)

    def test_rejects_naive_datetime(self) -> None:
        """A naive timestamp is ambiguous, and guessing would be silent."""
        with pytest.raises(ValueError, match="timezone-aware"):
            paths.encode_as_of(datetime(2026, 8, 1, 3, 30))

    def test_encoding_sorts_chronologically(self) -> None:
        """latest_partition() relies on lexicographic order matching time order."""
        moments = [
            MOMENT,
            MOMENT + timedelta(seconds=1),
            MOMENT + timedelta(days=40),
            MOMENT + timedelta(days=400),
        ]
        encoded = [paths.encode_as_of(m) for m in moments]
        assert sorted(encoded) == encoded

    def test_decode_returns_utc_aware(self) -> None:
        assert paths.decode_as_of("2026-08-01T03-30-00Z").tzinfo is UTC


class TestRawPaths:
    def test_expected_layout(self, isolated_data_root: Path) -> None:
        assert paths.raw_partition("fpl", "bootstrap-static", SEASON, MOMENT) == (
            isolated_data_root
            / "raw"
            / "fpl"
            / "bootstrap_static"
            / "season=2026-27"
            / "as_of=2026-08-01T03-30-00Z"
        )

    def test_hyphen_and_underscore_endpoints_are_the_same_partition(self) -> None:
        assert paths.raw_partition("fpl", "bootstrap-static", SEASON, MOMENT) == (
            paths.raw_partition("fpl", "bootstrap_static", SEASON, MOMENT)
        )

    def test_event_adds_a_partition_level(self, isolated_data_root: Path) -> None:
        assert paths.raw_partition("fpl", "event_live", SEASON, MOMENT, event=7).parent.parent == (
            isolated_data_root / "raw" / "fpl" / "event_live" / "season=2026-27"
        )
        assert (
            paths.raw_partition("fpl", "event_live", SEASON, MOMENT, event=7).parent.name
            == "event=7"
        )

    def test_data_root_override_wins_over_environment(self, tmp_path: Path) -> None:
        explicit = tmp_path / "explicit"
        result = paths.raw_partition("fpl", "fixtures", SEASON, MOMENT, data_root=explicit)
        assert result.is_relative_to(explicit)

    def test_no_path_component_contains_illegal_characters(self) -> None:
        path = paths.raw_partition("fpl", "event_live", SEASON, MOMENT, event=7)
        for part in path.parts[1:]:
            assert not set(part) & set('<>:"|?*'), part

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "", "..", "with:colon"])
    def test_rejects_path_traversal_and_illegal_names(self, bad: str) -> None:
        """A source name is never user input today, but a path-building
        function that can escape its root is a latent problem."""
        with pytest.raises(ValueError):
            paths.raw_partition(bad, "fixtures", SEASON, MOMENT)


class TestChunkPaths:
    def test_zero_padded_to_four_digits(self) -> None:
        """Zero padding keeps chunks in order when listed lexicographically."""
        assert paths.chunk_partition("fpl", "entry_picks", SEASON, 7, event=1).name == "chunk=0007"

    def test_rejects_negative_index(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            paths.chunk_partition("fpl", "entry_picks", SEASON, -1, event=1)

    def test_chunks_sort_lexicographically_in_numeric_order(self) -> None:
        names = [
            paths.chunk_partition("fpl", "entry_picks", SEASON, i, event=1).name
            for i in (0, 2, 9, 10, 100)
        ]
        assert sorted(names) == names


class TestLatestPartition:
    def test_returns_none_when_nothing_captured(self) -> None:
        assert paths.latest_partition("fpl", "bootstrap_static", SEASON) is None

    def test_returns_the_only_partition(self) -> None:
        expected = paths.raw_partition("fpl", "bootstrap_static", SEASON, MOMENT)
        expected.mkdir(parents=True)
        assert paths.latest_partition("fpl", "bootstrap_static", SEASON) == expected

    def test_returns_the_most_recent_of_many(self) -> None:
        for offset in (0, 1, 5, 2):
            paths.raw_partition(
                "fpl", "bootstrap_static", SEASON, MOMENT + timedelta(days=offset)
            ).mkdir(parents=True)
        latest = paths.latest_partition("fpl", "bootstrap_static", SEASON)
        assert latest is not None
        assert latest.name == f"as_of={paths.encode_as_of(MOMENT + timedelta(days=5))}"

    def test_ignores_unrelated_directories(self) -> None:
        parent = paths.raw_endpoint_dir("fpl", "bootstrap_static", SEASON)
        (parent / "chunk=0000").mkdir(parents=True)
        (parent / "notes").mkdir(parents=True)
        assert paths.latest_partition("fpl", "bootstrap_static", SEASON) is None

    def test_is_scoped_to_its_event(self) -> None:
        paths.raw_partition("fpl", "event_live", SEASON, MOMENT, event=1).mkdir(parents=True)
        assert paths.latest_partition("fpl", "event_live", SEASON, event=2) is None
        assert paths.latest_partition("fpl", "event_live", SEASON, event=1) is not None


class TestIterChunks:
    def test_empty_when_nothing_captured(self) -> None:
        assert list(paths.iter_chunks("fpl", "entry_picks", SEASON, event=1)) == []

    def test_yields_indices_in_numeric_order(self) -> None:
        for index in (2, 0, 10, 1):
            paths.chunk_partition("fpl", "entry_picks", SEASON, index, event=1).mkdir(parents=True)
        assert [i for i, _ in paths.iter_chunks("fpl", "entry_picks", SEASON, event=1)] == [
            0,
            1,
            2,
            10,
        ]

    def test_ignores_as_of_partitions(self) -> None:
        paths.raw_partition("fpl", "entry_picks", SEASON, MOMENT, event=1).mkdir(parents=True)
        assert list(paths.iter_chunks("fpl", "entry_picks", SEASON, event=1)) == []


class TestStagedAndFactsPaths:
    def test_staged_layout(self, isolated_data_root: Path) -> None:
        assert paths.staged_table("players", SEASON) == (
            isolated_data_root / "staged" / "players" / "season=2026-27"
        )

    def test_facts_layout(self, isolated_data_root: Path) -> None:
        assert paths.facts_table("player_fixture", SEASON) == (
            isolated_data_root / "facts" / "player_fixture" / "season=2026-27"
        )

    def test_rules_partition_precedes_season(self, isolated_data_root: Path) -> None:
        """Coarser partition first: one ruleset spans every season it scores."""
        assert paths.facts_table("points", SEASON, rules="2026-27") == (
            isolated_data_root / "facts" / "points" / "rules=2026-27" / "season=2026-27"
        )

    def test_same_season_can_be_scored_under_several_rulesets(self) -> None:
        assert paths.facts_table("points", SEASON, rules="2025-26") != paths.facts_table(
            "points", SEASON, rules="2026-27"
        )
