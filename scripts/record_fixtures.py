"""Record trimmed FPL API payloads for offline tests.

Run manually, never in CI:

    uv run python scripts/record_fixtures.py

Payloads are trimmed to a handful of entries but keep **every key**, so schema
assertions stay meaningful while the repository does not carry half a megabyte
of JSON per test. Tests must never hit the network: they need to be fast,
deterministic, and runnable on a train.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fpl.config import CURRENT_SEASON
from fpl.sources.fpl_api import FplApiConnector

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "fpl"

# How many entries of each list to keep. Enough to exercise "more than one" and
# to include some variety, few enough to stay diffable.
KEEP = 5


def _trim_list(value: Any, keep: int = KEEP) -> Any:
    return value[:keep] if isinstance(value, list) else value


def trim_bootstrap(payload: dict[str, Any]) -> dict[str, Any]:
    trimmed = dict(payload)
    for key in ("elements", "teams", "element_stats", "element_types", "phases"):
        if key in trimmed:
            trimmed[key] = _trim_list(trimmed[key])
    # Events are kept whole: gameweek deadlines and the finished/data_checked
    # flags drive the capture-window logic in phase 3, and that logic is only
    # worth testing against a realistic 38-event calendar.
    return trimmed


def trim_fixtures(payload: list[Any]) -> list[Any]:
    return _trim_list(payload, keep=10)


def trim_event_live(payload: dict[str, Any]) -> dict[str, Any]:
    trimmed = dict(payload)
    trimmed["elements"] = _trim_list(trimmed.get("elements", []))
    return trimmed


def write(name: str, payload: Any) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{name}.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {path.relative_to(REPO_ROOT)} ({path.stat().st_size / 1024:.0f} KB)")


def main() -> None:
    with FplApiConnector(CURRENT_SEASON) as connector:
        bootstrap = json.loads(connector.bootstrap_static().body)
        write("bootstrap_static", trim_bootstrap(bootstrap))

        fixtures = json.loads(connector.fixtures().body)
        write("fixtures", trim_fixtures(fixtures))

        try:
            live = json.loads(connector.event_live(1).body)
        except Exception as exc:  # noqa: BLE001 - diagnostic script: report and carry on
            print(f"event/1/live unavailable ({exc}); expected before the season starts")
        else:
            # Before the season starts this is `{"elements": []}`. That is worth
            # keeping as its own fixture: it is exactly what a scheduled job will
            # meet every day until 21 August, so the pipeline has to handle it.
            name = "event_live_preseason" if not live.get("elements") else "event_live_1"
            write(name, trim_event_live(live))


if __name__ == "__main__":
    main()
