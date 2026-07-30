"""Atomic file writes.

Spec §10 rates a half-written parquet that looks complete as the worst possible
outcome: it is indistinguishable from a good one, so everything downstream
trusts it. Every write in this project goes through here, which means a killed
job leaves either the old file or the new one, never a blend of the two.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["atomic_write_bytes"]


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically, creating parent directories.

    The temporary file is created in the *same directory* as the target so the
    final rename stays within one filesystem, where it is atomic. A rename
    across filesystems degrades to a copy, which is exactly the non-atomic
    behaviour being avoided.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        # BaseException, not Exception: a KeyboardInterrupt or a SystemExit
        # mid-write must not leave a stray temp file behind either.
        tmp.unlink(missing_ok=True)
        raise
