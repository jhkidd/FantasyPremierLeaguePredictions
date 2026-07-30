"""The ``fpl`` command line interface.

Actions is a scheduler, not a second implementation. Every command here does
exactly the same thing on a laptop as it does in CI, and every command is
idempotent and safe to re-run.

Commands belonging to phases not yet built exit 2 with a note naming the phase,
so the intended surface is visible and honestly unfinished rather than absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from fpl import __version__, exit_codes, log
from fpl.config import CURRENT_SEASON, Season
from fpl.ingest import ingest_fpl
from fpl.sources.errors import BlockedError, SchemaError, SourceError

app = typer.Typer(
    name="fpl",
    help="Fantasy Premier League data layer.",
    no_args_is_help=True,
    add_completion=False,
)
crosswalk_app = typer.Typer(help="Manage identity mappings between sources.", no_args_is_help=True)
app.add_typer(crosswalk_app, name="crosswalk")


def _pending(phase: int, what: str) -> None:
    typer.secho(
        f"'{what}' is not implemented yet (planned for phase {phase}).",
        err=True,
        fg=typer.colors.YELLOW,
    )
    raise typer.Exit(exit_codes.NOT_IMPLEMENTED)


def _data_root(ctx: typer.Context) -> Path | None:
    return (ctx.obj or {}).get("data_root")


def _parse_season(value: str) -> Season:
    try:
        return Season.parse(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


SeasonOption = Annotated[
    str,
    typer.Option("--season", help="Season in the form 2026-27.", show_default=True),
]


@app.callback()
def main(
    ctx: typer.Context,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
    data_root: Annotated[
        Path | None,
        typer.Option("--data-root", help="Override the data directory."),
    ] = None,
) -> None:
    log.configure(verbose=verbose)
    ctx.obj = {"data_root": data_root}


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command()
def ingest(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Source name, e.g. 'fpl'.")],
    season: SeasonOption = str(CURRENT_SEASON),
    endpoint: Annotated[
        str | None,
        typer.Option("--endpoint", help="Endpoint to pull. Omit for the routine set."),
    ] = None,
    event: Annotated[int | None, typer.Option("--event", help="Gameweek number.")] = None,
    player: Annotated[int | None, typer.Option("--player", help="FPL element id.")] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Write even if the content is unchanged.")
    ] = False,
) -> None:
    """Pull from a source into data/raw/."""
    parsed = _parse_season(season)

    if source != "fpl":
        _pending(7, f"ingest {source}")

    try:
        results = ingest_fpl(
            parsed,
            [endpoint] if endpoint else None,
            event=event,
            player_id=player,
            data_root=_data_root(ctx),
            force=force,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except BlockedError as exc:
        # Distinct exit code so the workflow can raise an issue immediately
        # rather than retrying into the block (spec §10, §13).
        typer.secho(
            f"Blocked by the source: {exc} (cloudflare={exc.looks_like_cloudflare}). Not retrying.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(exit_codes.BLOCKED) from exc
    except SchemaError as exc:
        typer.secho(f"Source schema changed: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(exit_codes.SCHEMA_CHANGED) from exc
    except SourceError as exc:
        typer.secho(f"Fetch failed: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(exit_codes.FAILURE) from exc

    written = sum(1 for result in results if result.written)
    typer.echo(
        f"{len(results)} endpoint(s) pulled, {written} written, {len(results) - written} unchanged"
    )


@app.command()
def stage(
    source: Annotated[str, typer.Argument(help="Source name.")],
    season: SeasonOption = str(CURRENT_SEASON),
) -> None:
    """Transform data/raw/ into typed tables in data/staged/."""
    _parse_season(season)
    _pending(4, f"stage {source}")


@app.command()
def facts(
    season: SeasonOption = str(CURRENT_SEASON),
    rules: Annotated[str | None, typer.Option("--rules", help="Scoring ruleset.")] = None,
) -> None:
    """Assemble canonical player-fixture facts from data/staged/."""
    _parse_season(season)
    _pending(5, "facts")


@crosswalk_app.command("refresh")
def crosswalk_refresh(season: SeasonOption = str(CURRENT_SEASON)) -> None:
    """Propose new identity mappings for review."""
    _parse_season(season)
    _pending(6, "crosswalk refresh")


@crosswalk_app.command("validate")
def crosswalk_validate() -> None:
    """Fail if any player with minutes is unmapped."""
    _pending(6, "crosswalk validate")


@app.command()
def check() -> None:
    """Run every data quality gate."""
    _pending(4, "check")


@app.command()
def features(
    as_of: Annotated[str, typer.Option("--as-of", help="ISO 8601 instant, UTC.")],
    horizon: Annotated[int, typer.Option("--horizon", help="Gameweeks ahead.")] = 5,
) -> None:
    """Build features at a point in time. Inspection only — writes to scratch/."""
    _pending(8, "features")


@app.command()
def backfill(
    from_season: Annotated[str, typer.Option("--from")] = "2016-17",
    to_season: Annotated[str, typer.Option("--to")] = "2025-26",
) -> None:
    """One-off historical cold start."""
    _parse_season(from_season)
    _parse_season(to_season)
    _pending(6, "backfill")
