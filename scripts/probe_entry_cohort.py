"""Are early registrants skilled managers?

The overall league is empty until a gameweek is scored, so there is no top-1,000
to select for GW1. `entry/{id}/history/` exposes prior-season ranks, and entry
IDs are allocated in registration order, so *if* early registrants are
meaningfully stronger than average this gives a cohort that can be identified
before the GW1 deadline rather than after it.

Baseline for comparison: 2.28m entries, so a uniformly random manager has a
median rank around 1.14m.
"""

from __future__ import annotations

import statistics
import sys
import time

import httpx

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "FantasyPremierLeaguePredictions/0.1 (probe)"}
LAST_SEASON = "2025/26"


def past_rank(client: httpx.Client, entry_id: int) -> tuple[int | None, int]:
    """Returns (2025/26 rank or None, number of past seasons)."""
    response = client.get(f"{BASE}/entry/{entry_id}/history/")
    if response.status_code != 200:
        return None, -1
    past = response.json().get("past", [])
    rank = next((p["rank"] for p in past if p["season_name"] == LAST_SEASON), None)
    return rank, len(past)


def sample(client: httpx.Client, ids: list[int], label: str) -> None:
    ranks: list[int] = []
    veterans = 0
    missing = 0
    for entry_id in ids:
        rank, seasons = past_rank(client, entry_id)
        if seasons > 0:
            veterans += 1
        if rank is None:
            missing += 1
        else:
            ranks.append(rank)
        time.sleep(1.0)

    print(f"\n--- {label} (n={len(ids)}) ---")
    print(
        f"  played last season: {len(ranks)}/{len(ids)}   any history: {veterans}   new: {missing}"
    )
    if ranks:
        ranks.sort()
        print(f"  median rank: {statistics.median(ranks):,.0f}")
        print(f"  best: {ranks[0]:,}   worst: {ranks[-1]:,}")
        print(f"  in top 100k: {sum(1 for r in ranks if r <= 100_000)}/{len(ranks)}")
        print(f"  in top 10k:  {sum(1 for r in ranks if r <= 10_000)}/{len(ranks)}")


def main() -> int:
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        sample(client, list(range(1, 26)), "earliest registrants (ids 1-25)")
        sample(client, list(range(1_000_000, 1_000_025)), "mid registrants (ids 1.0m)")
        sample(client, list(range(2_200_000, 2_200_025)), "late registrants (ids 2.2m)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
