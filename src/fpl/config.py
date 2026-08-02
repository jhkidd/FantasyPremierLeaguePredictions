"""Configuration: paths, seasons, and per-source politeness settings.

Pure values only. Nothing in this module performs I/O or network access, so
importing it is always cheap and side-effect free.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

__all__ = [
    "CURRENT_SEASON",
    "DEFAULT_ELITE_COHORT_SIZE",
    "FIRST_ARCHIVE_SEASON",
    "FOOTBALL_DATA_API_KEY_ENV_VAR",
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
    # European competition fixtures, fetched as a single tarball like vaastav
    # (plan §7.4-7.5). Not a per-file sweep, so a generous interval costs
    # nothing.
    "openfootball": SourceConfig("openfootball", min_request_interval=1.0, timeout=120.0),
    # football-data.org's free tier is documented at 10 requests/minute for
    # registered clients (spec §13). 6.0s enforces that proactively, rather
    # than relying on 429 backoff after the fact (plan §7.1/R3).
    "footballdataorg": SourceConfig("footballdataorg", min_request_interval=6.0, timeout=30.0),
}

DEFAULT_DATA_ROOT: Final = Path("data")
DATA_ROOT_ENV_VAR: Final = "FPL_DATA_ROOT"
MINI_LEAGUE_ENV_VAR: Final = "FPL_MINI_LEAGUE_ID"
ENTRY_ENV_VAR: Final = "FPL_ENTRY_ID"
FOOTBALL_DATA_API_KEY_ENV_VAR: Final = "FOOTBALL_DATA_API_KEY"
"""football-data.org's free-tier key (plan §7.1). The one credential in this
project (spec §13 amendment) - required only for FA Cup / EFL Cup fixtures,
never for any other source."""

DEFAULT_ELITE_COHORT_SIZE: Final = 1000
"""Entries sampled from the overall league. A cheaper stand-in for the top-10k
benchmark community tools use; the bias is known and consistent, which is what
matters for a feature used comparatively (spec §6.1)."""


@dataclass(frozen=True)
class Config:
    data_root: Path
    user_agent: str = USER_AGENT
    mini_league_id: int | None = None
    """The user's own mini-league. Configuration rather than a constant because
    it differs per user and is not known until they join (spec §6.1)."""

    entry_id: int | None = None
    """The user's own team. Needed to capture what we actually played, and to
    discover which private leagues we are in without being told."""

    football_data_api_key: str | None = field(default=None, repr=False)
    """football-data.org's free-tier key, for FA Cup / EFL Cup fixtures only
    (plan §7.1). Never logged, never embedded in a URL or raw-artifact
    metadata - the connector reads it straight from here and sends it as an
    ``X-Auth-Token`` header. ``repr=False`` so it can never end up in a log
    line via an accidental ``repr(config)``."""

    @classmethod
    def load(cls) -> Config:
        """Build config from the environment.

        Reads the environment on every call rather than caching, so tests can
        redirect ``FPL_DATA_ROOT`` at a temporary directory and be certain they
        never touch the real committed data tree.
        """
        raw_root = os.environ.get(DATA_ROOT_ENV_VAR)
        data_root = Path(raw_root) if raw_root else DEFAULT_DATA_ROOT
        return cls(
            data_root=data_root,
            mini_league_id=_read_positive_int(MINI_LEAGUE_ENV_VAR),
            entry_id=_read_positive_int(ENTRY_ENV_VAR),
            football_data_api_key=_read_secret(FOOTBALL_DATA_API_KEY_ENV_VAR),
        )

    def source(self, name: str) -> SourceConfig:
        try:
            return SOURCES[name]
        except KeyError:
            raise KeyError(f"unknown source {name!r}; known: {sorted(SOURCES)}") from None


def _read_positive_int(name: str) -> int | None:
    """A malformed ID is ignored rather than fatal.

    These come from workflow configuration, and a typo there must not take down
    the daily snapshot, which uses neither. Capture surfaces an absence
    explicitly at the point it actually needs one.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _read_secret(name: str) -> str | None:
    """A blank or unset environment variable is treated as ``None``.

    Unlike :func:`_read_positive_int`, a malformed value cannot exist here -
    any non-blank string is a plausible key. Whether it is a *valid* key is
    for football-data.org to say, at request time (plan §7.8/R4) - never
    silently, and never by falling back to "source skipped".
    """
    raw = os.environ.get(name, "").strip()
    return raw or None
