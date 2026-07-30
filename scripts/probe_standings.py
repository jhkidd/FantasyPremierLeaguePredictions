"""Follow-up probe: what does the overall league look like pre-season?

The first probe found `leagues-classic/314/standings/` returning zero results,
which would break the top-1,000 selection that ownership capture depends on.
This establishes whether that is a pre-season condition or a permanent one.
"""

from __future__ import annotations

import json
import sys

import httpx

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "FantasyPremierLeaguePredictions/0.1 (probe)"}


def get(client: httpx.Client, path: str):
    response = client.get(f"{BASE}{path}")
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text[:300]


def main() -> int:
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        status, body = get(client, "/leagues-classic/314/standings/")
        print(f"--- standings ({status}) ---")
        print(json.dumps(body, indent=2)[:1500])

        print("\n--- with phase params ---")
        for query in ("?phase=1", "?page_standings=1&phase=1", "?page_new_entries=1"):
            status, body = get(client, f"/leagues-classic/314/standings/{query}")
            count = len(body.get("standings", {}).get("results", [])) if isinstance(body, dict) else "-"
            new = len(body.get("new_entries", {}).get("results", [])) if isinstance(body, dict) else "-"
            print(f"{status}  {query}  standings={count} new_entries={new}")

        print("\n--- entry/1 history ---")
        status, body = get(client, "/entry/1/history/")
        if isinstance(body, dict):
            print(f"current={len(body.get('current', []))} past={len(body.get('past', []))}")
            print(f"past sample: {json.dumps(body.get('past', [])[-3:])}")

        print("\n--- entry/1 summary ---")
        status, body = get(client, "/entry/1/")
        if isinstance(body, dict):
            for key in ("id", "name", "current_event", "entered_events", "joined_time"):
                value = body.get(key)
                if isinstance(value, list):
                    value = f"list len={len(value)}"
                print(f"  {key}={value}")

        print("\n--- total players registered ---")
        status, body = get(client, "/bootstrap-static/")
        if isinstance(body, dict):
            print(f"  total_players={body.get('total_players')}")
            events = body.get("events", [])
            live = [e for e in events if e.get("is_current")]
            nxt = [e for e in events if e.get("is_next")]
            print(f"  is_current={[e['id'] for e in live]} is_next={[e['id'] for e in nxt]}")
            if events:
                first = events[0]
                print(f"  event1: {json.dumps({k: first[k] for k in ('id','deadline_time','finished','data_checked','is_previous','is_current','is_next')})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
