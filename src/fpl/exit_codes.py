"""Process exit codes.

Workflows branch on these, so they are a contract rather than an implementation
detail. In particular a 403 must be distinguishable from an ordinary failure:
spec §10 requires that being blocked raises an issue immediately instead of
being retried into the block.

0, 1 and 2 are left alone — 2 is Click's usage error, and colliding with it
would make "you typed the command wrong" indistinguishable from anything else.
"""

from __future__ import annotations

from typing import Final

SUCCESS: Final = 0
FAILURE: Final = 1
USAGE: Final = 2

NOT_IMPLEMENTED: Final = 10
"""The command exists but its phase has not been built yet."""

BLOCKED: Final = 11
"""HTTP 403. The runner is blocked, not merely unlucky. Never retry."""

SCHEMA_CHANGED: Final = 12
"""A source returned a shape we do not recognise. Nothing downstream should run."""

QUALITY_GATE_FAILED: Final = 13
"""Data was fetched and parsed but failed a sanity check. Do not commit it."""
