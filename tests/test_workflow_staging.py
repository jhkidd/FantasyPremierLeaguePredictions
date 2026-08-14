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
        """No `continue-on-error` on the staging step: a red run is the signal."""
        assert "continue-on-error" not in _text()
