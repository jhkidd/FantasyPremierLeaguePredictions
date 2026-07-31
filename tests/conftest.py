from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point FPL_DATA_ROOT at a temp directory for every test.

    Autouse and unconditional: a test that accidentally writes to the real
    committed ``data/`` tree would be both destructive and very hard to notice.
    """
    root = tmp_path / "data"
    monkeypatch.setenv("FPL_DATA_ROOT", str(root))
    yield root


@pytest.fixture(autouse=True)
def _no_ambient_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset the team and league variables for every test.

    Both are read from the environment, so a developer who exports their own
    would silently change what the tests exercise — and the failure would look
    like a code bug rather than a shell setting. Tests that want them set them.
    """
    for name in ("FPL_ENTRY_ID", "FPL_MINI_LEAGUE_ID"):
        monkeypatch.delenv(name, raising=False)
