"""The scheduled snapshot workflow stages what it ingests.

`daily-snapshot.yml` originally ran `fpl ingest fpl` and committed `data/`,
with no staging step at all. Raw partitions therefore accumulated daily while
the staged layer stood still: by the time it was noticed, raw held nineteen
`bootstrap_static` captures and staged held nine — ten days of pre-season
price and availability movement sitting unread.

Nothing was lost, because raw is the durable record and staging is a pure
function of it. But the drift was silent, recurred every single day, and was
only caught by someone happening to look. These tests make the ordering
invariant explicit so it cannot quietly lapse again.

Asserted against the file text rather than a parsed document on purpose:
PyYAML is not a dependency of this project and the dependency list is
deliberately curated (see pyproject.toml), so a regression guard for one
workflow file does not justify adding one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
DAILY_SNAPSHOT = WORKFLOWS / "daily-snapshot.yml"

pytestmark = pytest.mark.skipif(
    not DAILY_SNAPSHOT.is_file(), reason="workflow not present in this checkout"
)


def _text() -> str:
    return DAILY_SNAPSHOT.read_text(encoding="utf-8")


def _step_block(name: str) -> str:
    """The text of one step, from its ``- name: <name>`` line up to (but not
    including) the next step's ``- name:`` line.

    Matches on a trailing newline so ``name="Stage"`` finds the core
    ``- name: Stage`` step without also matching ``- name: Stage
    openfootball`` or any other step whose name merely starts with it.
    """
    text = _text()
    marker = f"      - name: {name}\n"
    start = text.index(marker)
    rest = text[start:]
    next_step = rest.find("\n      - name:", len(marker))
    return rest if next_step == -1 else rest[:next_step]


class TestDailySnapshotStages:
    """Ingesting without staging is the bug; these pin the fix."""

    def test_ingests_fpl(self) -> None:
        assert "ingest fpl" in _text()

    def test_stages_fpl(self) -> None:
        """The step whose absence caused ten days of unstaged captures."""
        assert "stage fpl" in _text()

    def test_stages_after_ingesting(self) -> None:
        """Staging before ingesting would stage yesterday's raw, forever a day behind."""
        text = _text()
        assert text.index("ingest fpl") < text.index("stage fpl")

    def test_commits_raw_before_staging(self) -> None:
        """Raw is unrecoverable; staged is rebuildable from it.

        A staging failure must therefore never be able to cost a day of raw
        data, which means raw is committed and pushed before staging is even
        attempted.
        """
        text = _text()
        assert text.index("git add data/raw/") < text.index("stage fpl")

    def test_commits_staged_after_staging(self) -> None:
        text = _text()
        assert text.index("stage fpl") < text.index("git add data/staged/")

    def test_does_not_commit_data_wholesale(self) -> None:
        """`git add data/` in the raw step would sweep staged into the raw commit.

        That would defeat the split: the whole point is that the raw commit
        lands even when staging later fails.
        """
        assert "git add data/\n" not in _text()


class TestStagingFailureIsVisible:
    """Recoverable, but not to be shrugged off."""

    def test_staging_step_is_not_permitted_to_fail_silently(self) -> None:
        """No `continue-on-error` on the core staging step: a red run there is
        the signal.

        Scoped to just the `Stage` step rather than the whole file: Phase 7
        (plan §7) deliberately added `continue-on-error: true` to the Tier 2
        ingest/stage steps (openfootball, football-data.co.uk, Understat) so
        one flaky source can't block the others, surfacing failure via a
        GitHub issue instead (see the "Raise an issue" step). That is an
        intentional, different failure-handling choice for those steps, not
        a silent-failure regression of this one.
        """
        assert "continue-on-error" not in _step_block("Stage")

    def test_tier2_steps_are_still_allowed_to_continue_on_error(self) -> None:
        """The opposite regression: this fix must not accidentally strip
        Tier 2's own `continue-on-error`, which is what lets one flaky
        source fail without blocking the others (plan §7)."""
        for name in ("Ingest openfootball", "Stage openfootball", "Stage Understat"):
            assert "continue-on-error: true" in _step_block(name)
