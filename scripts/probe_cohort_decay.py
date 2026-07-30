"""How far does the "early registrant = strong manager" signal extend?

Determines the cost of building a prior-season-rank cohort before the GW1
deadline: if the signal decays by id 20k we scan 20k ids, if it holds to 200k
the scan is ten times more expensive.
"""

from __future__ import annotations

import statistics
import sys
import time

import httpx

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "FantasyPremierLeaguePredictions/0.1 (probe)"}
LAST_SEASON = "2025/26"
BANDS = [200, 1_000, 5_000, 20_000, 60_000, 150_000, 400_000]
PER_BAND = 20


def past_rank(client: httpx.Client, entry_id: int) -> int | None:
    response = client.get(f"{BASE}/entry/{entry_id}/history/")
    if response.status_code != 200:
        return None
    past = response.json().get("past", [])
    return next((p["rank"] for p in past if p["season_name"] == LAST_SEASON), None)


def main() -> int:
    print(f"{'band':>9} {'played':>7} {'median':>12} {'top100k':>8} {'top10k':>7}")
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        for start in BANDS:
            ranks = []
            for entry_id in range(start, start + PER_BAND):
                rank = past_rank(client, entry_id)
                if rank is not None:
                    ranks.append(rank)
                time.sleep(0.8)
            if ranks:
                median = f"{statistics.median(ranks):,.0f}"
                top100k = sum(1 for r in ranks if r <= 100_000)
                top10k = sum(1 for r in ranks if r <= 10_000)
            else:
                median, top100k, top10k = "-", 0, 0
            print(
                f"{start:>9,} {len(ranks):>3}/{PER_BAND:<3} {median:>12} {top100k:>8} {top10k:>7}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
