# Fantasy Premier League Predictions

A system for picking a winning Fantasy Premier League team: models that project
player points, an optimiser that selects squads and transfers, and a static site
for making decisions before each gameweek deadline.

Models are trained locally. GitHub Actions pulls fresh data, runs inference, and
commits the results, which the site reads.

## Status

Subsystem 1 (ingestion and feature store) is in development.

| Doc | Purpose |
|---|---|
| [Design spec](docs/superpowers/specs/2026-07-30-fpl-data-layer-design.md) | Architecture, data sources, contracts |
| [Implementation plan](docs/superpowers/plans/2026-07-30-fpl-data-layer-phases-1-3-plan.md) | Phases 1–3 in detail, 4–9 sketched |

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync                  # create the environment from uv.lock
uv run pytest            # tests
uv run ruff check .      # lint
uv run ruff format .     # format
uv run fpl --help        # the CLI
```

## Data

Data lives in `data/`, committed to this repository:

| Layer | Contents |
|---|---|
| `data/raw/` | Exactly what each source returned, gzipped. Append-only. |
| `data/staged/` | Typed, deduplicated tables. Rebuildable from `raw/`. |
| `data/facts/` | Canonical one row per (player, fixture). Rebuildable from `staged/`. |

Features are not stored — they are a library computed on demand at a given
`as_of` timestamp, so training and inference share one implementation.

All sources are public and require no credentials.
