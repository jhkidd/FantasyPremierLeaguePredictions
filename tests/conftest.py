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
