"""Probe the manager endpoints (plan 3.1).

`entry/{id}/event/{gw}/picks/` is the one endpoint the design could not verify:
2026/27 had not started and prior-season picks were believed not to be
retained. If it needs authentication, that is a design-level problem and we
need to find out with days of slack, not hours.

Run: uv run python scripts/probe_manager_endpoints.py
"""

from __future__ import annotations

import json
import sys

import httpx

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "FantasyPremierLeaguePredictions/0.1 (probe)"}


def probe(client: httpx.Client, path: str) -> tuple[int, object]:
    response = client.get(f"{BASE}{path}")
    try:
        body = response.json()
    except ValueError:
        body = response.text[:200]
    return response.status_code, body


def summarise(body: object) -> str:
    if isinstance(body, dict):
        return f"dict keys={sorted(body)[:12]}"
    if isinstance(body, list):
        return f"list len={len(body)}"
    return repr(body)[:200]


def main() -> int:
    paths = [
        "/leagues-classic/314/standings/",
        "/leagues-classic/314/standings/?page_standings=2",
        "/entry/1/",
        "/entry/1/history/",
        # Last gameweek of 2025/26: does FPL still serve the previous season?
        "/entry/1/event/38/picks/",
        # This season's GW1, which has not been played.
        "/entry/1/event/1/picks/",
    ]
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        results = {}
        for path in paths:
            status, body = probe(client, path)
            results[path] = (status, body)
            print(f"{status}  {path}\n      {summarise(body)}")

    standings = results["/leagues-classic/314/standings/"][1]
    if isinstance(standings, dict):
        page = standings.get("standings", {})
        entries = page.get("results", [])
        print(f"\nstandings: {len(entries)} results per page, has_next={page.get('has_next')}")
        if entries:
            print(f"first entry keys: {sorted(entries[0])}")
            print(f"first entry: {json.dumps(entries[0])[:300]}")

    picks = results["/entry/1/event/38/picks/"][1]
    if isinstance(picks, dict) and "picks" in picks:
        print(f"\n2025/26 picks ARE retained. keys={sorted(picks)}")
        print(f"picks[0]={json.dumps(picks['picks'][0])}")
        print(f"automatic_subs len={len(picks.get('automatic_subs', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
