"""Configuration: paths, seasons, and per-source politeness settings.

Pure values only. Nothing in this module performs I/O or network access, so
importing it is always cheap and side-effect free.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "CURRENT_SEASON",
    "FIRST_ARCHIVE_SEASON",
    "SOURCES",
    "USER_AGENT",
    "Config",
    "Season",
    "SourceConfig",
]

_SEASON_RE: Final = re.compile(r"^(\d{4})-(\d{2})$")


@dataclass(frozen=True, order=True)
class Season:
    """An English football season, identified by the calendar year it starts in.

    Stored as a single integer so that seasons sort and compare naturally.
    The canonical string form is ``"2026-27"``.
    """

    start_year: int

    def __post_init__(self) -> None:
        if not 1888 <= self.start_year <= 2200:
            raise ValueError(f"implausible season start year: {self.start_year}")

    @classmethod
    def parse(cls, text: str) -> Season:
        match = _SEASON_RE.match(text.strip())
        if match is None:
            raise ValueError(f"malformed season {text!r}; expected a form like '2026-27'")
        start_year = int(match.group(1))
        expected_end = (start_year + 1) % 100
        if int(match.group(2)) != expected_end:
            raise ValueError(
                f"inconsistent season {text!r}; {start_year} should be "
                f"followed by {expected_end:02d}"
            )
        return cls(start_year)

    @property
    def end_year(self) -> int:
        return self.start_year + 1

    def __str__(self) -> str:
        return f"{self.start_year}-{self.end_year % 100:02d}"


CURRENT_SEASON: Final = Season(2026)
FIRST_ARCHIVE_SEASON: Final = Season(2016)

# Identifies our traffic to the sources we depend on. Spec §13: a descriptive
# user-agent is good practice regardless, and it makes our requests legible if
# anyone upstream ever looks at who is calling them.
USER_AGENT: Final = (
    "FantasyPremierLeaguePredictions/0.1 "
    "(+https://github.com/jhkidd/FantasyPremierLeaguePredictions)"
)


@dataclass(frozen=True)
class SourceConfig:
    """Politeness and resilience settings for one external source."""

    name: str
    min_request_interval: float
    """Seconds to leave between consecutive requests to this source."""

    timeout: float
    max_attempts: int = 4


SOURCES: Final[dict[str, SourceConfig]] = {
    # The FPL API publishes no rate limit. 2s is the community norm for routine
    # use; backfill sweeps hundreds of requests, so they go slower still.
    "fpl": SourceConfig("fpl", min_request_interval=2.0, timeout=30.0),
    "fpl_backfill": SourceConfig("fpl_backfill", min_request_interval=3.0, timeout=30.0),
    "understat": SourceConfig("understat", min_request_interval=2.0, timeout=30.0),
    "clubelo": SourceConfig("clubelo", min_request_interval=2.0, timeout=30.0),
    "footballdata": SourceConfig("footballdata", min_request_interval=2.0, timeout=60.0),
    "vaastav": SourceConfig("vaastav", min_request_interval=1.0, timeout=120.0),
}

DEFAULT_DATA_ROOT: Final = Path("data")
DATA_ROOT_ENV_VAR: Final = "FPL_DATA_ROOT"


@dataclass(frozen=True)
class Config:
    data_root: Path
    user_agent: str = USER_AGENT

    @classmethod
    def load(cls) -> Config:
        """Build config from the environment.

        Reads the environment on every call rather than caching, so tests can
        redirect ``FPL_DATA_ROOT`` at a temporary directory and be certain they
        never touch the real committed data tree.
        """
        raw_root = os.environ.get(DATA_ROOT_ENV_VAR)
        data_root = Path(raw_root) if raw_root else DEFAULT_DATA_ROOT
        return cls(data_root=data_root)

    def source(self, name: str) -> SourceConfig:
        try:
            return SOURCES[name]
        except KeyError:
            raise KeyError(f"unknown source {name!r}; known: {sorted(SOURCES)}") from None
