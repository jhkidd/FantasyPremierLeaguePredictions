"""Parse openfootball's `football.txt` plain-text match-list format.

Not CSV — a hand-formatted plain-text report, confirmed live against
`openfootball/champions-league`'s `2025-26/cl.txt` and `clq.txt` during
phase 7 probing. Three line shapes matter, everything else is decoration to
be skipped:

- A round header, e.g. ``▪ League, Matchday 1`` or ``▪ Finals, Final`` —
  every fixture until the next one belongs to this round.
- A date header, e.g. ``Tue Sep 16 2025`` (year given) or ``Wed Sep 17``
  (year omitted — carried forward from the most recent explicit year, with
  a same-season December->January rollover handled explicitly, since a
  European campaign always spans a single calendar-year boundary).
- A fixture line, e.g. ``18:45  Athletic Club (ESP)  v  Arsenal FC (ENG)
  0-2 (0-0)`` — the kickoff time is only printed once per group of
  simultaneous fixtures, so a fixture line with no leading time inherits the
  most recent date header, not a time (kickoff time itself is not part of
  this module's output; nothing downstream needs finer than day granularity
  for fixture-congestion counting).

Scores, half-time splits, and shootout/extra-time annotations (seen live,
e.g. ``11-10 pen. 2-0 a.e.t. (2-0, 2-0)``) are deliberately not parsed —
this connector exists for fixture *scheduling* (congestion counting), never
match outcomes, so nothing after a fixture's two team names is read at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from fpl.sources.errors import SchemaError

__all__ = ["ParsedFixture", "parse_football_txt"]

_ROUND_MARKER = "▪"

_MONTHS = {
    name: index
    for index, name in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}

_WEEKDAYS = frozenset({"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"})

_DATE_RE = re.compile(
    r"^(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(?P<month>[A-Z][a-z]{2})\s+"
    r"(?P<day>\d{1,2})(?:\s+(?P<year>\d{4}))?$"
)

_FIXTURE_RE = re.compile(
    r"^(?:\d{1,2}:\d{2}\s+)?"
    r"(?P<home>.+?\([A-Z]{3}\))\s+v\s+(?P<away>.+?\([A-Z]{3}\))(?:\s{2,}.*)?$"
)

_TEAM_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<country>[A-Z]{3})\)$")


@dataclass(frozen=True)
class ParsedFixture:
    """One fixture, exactly as the schedule states it — no score, no time."""

    match_date: date
    round: str
    home_team: str
    home_country: str
    away_team: str
    away_country: str


def _parse_team(raw: str) -> tuple[str, str]:
    match = _TEAM_RE.match(raw.strip())
    if match is None:
        raise SchemaError(f"could not parse team name and country from {raw!r}")
    return match.group("name").strip(), match.group("country")


def parse_football_txt(text: str, *, source_label: str = "football.txt") -> list[ParsedFixture]:
    """Every fixture in one `football.txt`-format document, in file order.

    ``source_label`` is only used to make a raised :class:`SchemaError`
    identify which file was being parsed — the parser itself is agnostic to
    which competition or season it is reading.
    """
    fixtures: list[ParsedFixture] = []
    current_round: str | None = None
    current_date: date | None = None
    current_year: int | None = None
    current_month: int | None = None

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith(_ROUND_MARKER):
            current_round = line.removeprefix(_ROUND_MARKER).strip()
            continue

        date_match = _DATE_RE.match(line)
        if date_match is not None:
            month = _MONTHS[date_match.group("month")]
            day = int(date_match.group("day"))
            year_text = date_match.group("year")
            if year_text is not None:
                year = int(year_text)
            elif current_year is None:
                raise SchemaError(
                    f"{source_label}:{line_number}: date {line!r} has no year and none "
                    "has been seen yet"
                )
            else:
                # A season-spanning fixture list only ever moves forward in
                # time. A month number smaller than the last one seen means
                # the calendar has rolled over into the following year -
                # e.g. "Dec ... 2025" then a bare "Jan 5" is 2026, not 2025.
                year = (
                    current_year + 1
                    if current_month is not None and month < current_month
                    else current_year
                )
            current_year = year
            current_month = month
            current_date = date(year, month, day)
            continue

        fixture_match = _FIXTURE_RE.match(line)
        if fixture_match is not None:
            if current_date is None:
                raise SchemaError(
                    f"{source_label}:{line_number}: fixture {line!r} appears before any date header"
                )
            if current_round is None:
                raise SchemaError(
                    f"{source_label}:{line_number}: fixture {line!r} "
                    "appears before any round header"
                )
            home_team, home_country = _parse_team(fixture_match.group("home"))
            away_team, away_country = _parse_team(fixture_match.group("away"))
            fixtures.append(
                ParsedFixture(
                    match_date=current_date,
                    round=current_round,
                    home_team=home_team,
                    home_country=home_country,
                    away_team=away_team,
                    away_country=away_country,
                )
            )
            continue

        # Anything else (the "=" title line, "#" metadata lines) is
        # decoration this parser has no use for.

    return fixtures
