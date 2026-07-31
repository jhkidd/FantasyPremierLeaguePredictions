"""The ``fpl`` command line interface.

Actions is a scheduler, not a second implementation. Every command here does
exactly the same thing on a laptop as it does in CI, and every command is
idempotent and safe to re-run.

Commands belonging to phases not yet built exit 2 with a note naming the phase,
so the intended surface is visible and honestly unfinished rather than absent.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from fpl import __version__, exit_codes, log
from fpl.config import (
    CURRENT_SEASON,
    DEFAULT_ELITE_COHORT_SIZE,
    ENTRY_ENV_VAR,
    MINI_LEAGUE_ENV_VAR,
    Config,
    Season,
)
from fpl.ingest import ingest_fpl
from fpl.ownership import (
    COHORTS,
    ELITE_COHORT,
    ELITE_FIRST_EVENT,
    MINI_COHORT,
    SELF_COHORT,
    CaptureTarget,
    capture_ownership,
    current_bootstrap,
    discover_private_leagues,
    elite_target,
    mini_target,
    resolve_capture_event,
    self_target,
)
from fpl.sources.errors import BlockedError, SchemaError, SourceError
from fpl.sources.fpl_api import FplApiConnector
from fpl.storage import paths

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


@contextmanager
def _source_failures() -> Iterator[None]:
    """Map source failures onto the exit-code contract.

    Workflows branch on these: a block needs a human and must not be retried, a
    schema change needs a code change, everything else is worth retrying.
    """
    try:
        yield
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except BlockedError as exc:
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
    entry: Annotated[
        int | None,
        typer.Option("--entry", help=f"Your own team ID. Defaults to ${ENTRY_ENV_VAR}."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Write even if the content is unchanged.")
    ] = False,
) -> None:
    """Pull from a source into data/raw/."""
    parsed = _parse_season(season)

    if source != "fpl":
        _pending(7, f"ingest {source}")

    entry_id = entry if entry is not None else Config.load().entry_id
    if endpoint is None and entry_id is None:
        typer.secho(
            f"No team configured (${ENTRY_ENV_VAR} unset); skipping the entry endpoint.",
            err=True,
            fg=typer.colors.YELLOW,
        )

    try:
        results = ingest_fpl(
            parsed,
            [endpoint] if endpoint else None,
            event=event,
            player_id=player,
            entry_id=entry_id,
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


@app.command("capture-ownership")
def capture_ownership_command(
    ctx: typer.Context,
    season: SeasonOption = str(CURRENT_SEASON),
    cohort: Annotated[
        str, typer.Option("--cohort", help="'self', 'mini', 'elite', or 'all'.")
    ] = "all",
    event: Annotated[
        int | None,
        typer.Option("--event", help="Gameweek. Omit to resolve the open one automatically."),
    ] = None,
    top: Annotated[
        int, typer.Option("--top", help="Elite cohort size.")
    ] = DEFAULT_ELITE_COHORT_SIZE,
    league: Annotated[
        int | None,
        typer.Option("--league", help=f"Mini-league ID. Defaults to ${MINI_LEAGUE_ENV_VAR}."),
    ] = None,
    entry: Annotated[
        int | None,
        typer.Option("--entry", help=f"Your own team ID. Defaults to ${ENTRY_ENV_VAR}."),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Cap entries per cohort, for rehearsal.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Resolve the target and print the plan only.")
    ] = False,
) -> None:
    """Capture rival squads for the open gameweek (spec §6.1).

    Runs every 30 minutes and does nothing on almost every invocation. Exits 0
    when there is no open gameweek, because "nothing to capture" is the normal
    state, not a failure.
    """
    parsed = _parse_season(season)
    if cohort not in {*COHORTS, "all"}:
        raise typer.BadParameter(f"unknown cohort {cohort!r}; expected {', '.join(COHORTS)} or all")

    data_root = _data_root(ctx)
    config = Config.load()
    entry_id = entry if entry is not None else config.entry_id
    league_id = league if league is not None else config.mini_league_id

    bootstrap = current_bootstrap(parsed, data_root=data_root)
    if bootstrap is None:
        typer.secho(
            "bootstrap-static is unavailable live and nothing is stored. "
            "Run 'fpl ingest fpl' first.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(exit_codes.FAILURE)

    if league_id is None and entry_id is not None and cohort in {MINI_COHORT, "all"}:
        # Only when a mini capture would otherwise go ahead. The job ticks 48
        # times a day and this is a network call; running it against a closed
        # gameweek would be 48 pointless requests for an answer nothing uses.
        pending = event or _open_event(parsed, bootstrap, MINI_COHORT, 1, data_root=data_root)
        if pending is not None:
            league_id = _discover_league(parsed, entry_id)

    targets = _capture_targets(
        parsed,
        bootstrap,
        cohort,
        event=event,
        top=top,
        league_id=league_id,
        entry_id=entry_id,
        data_root=data_root,
    )
    if not targets:
        typer.echo("nothing_to_do: no gameweek is open for capture")
        raise typer.Exit(exit_codes.SUCCESS)

    for target in targets:
        if dry_run:
            typer.echo(
                f"would capture cohort={target.cohort} league={target.league_id} "
                f"event={target.event} top={target.top} limit={limit}"
            )
            continue
        with _source_failures():
            outcome = capture_ownership(parsed, target, data_root=data_root, limit=limit)
        typer.echo(
            f"cohort={outcome.target.cohort} event={outcome.target.event} "
            f"entries={outcome.entries} chunks_written={outcome.chunks_written} "
            f"resumed={outcome.chunks_skipped} contaminated={outcome.contaminated}"
        )


def _capture_targets(
    season: Season,
    bootstrap: dict,
    cohort: str,
    *,
    event: int | None,
    top: int,
    league_id: int | None,
    entry_id: int | None,
    data_root: Path | None,
) -> list[CaptureTarget]:
    """Decide what to capture, honouring each cohort's earliest gameweek.

    The cohorts resolve their gameweek independently: the elite cohort cannot
    start before gameweek 2 because the overall league has no ranking until one
    has been scored, while the other two are readable from gameweek 1.
    """
    targets: list[CaptureTarget] = []

    if cohort in {SELF_COHORT, "all"}:
        if entry_id is None:
            if cohort == SELF_COHORT:
                raise typer.BadParameter(
                    f"no team configured; pass --entry or set ${ENTRY_ENV_VAR}"
                )
            typer.secho(
                f"No team configured (${ENTRY_ENV_VAR} unset); skipping that cohort.",
                err=True,
                fg=typer.colors.YELLOW,
            )
        else:
            resolved = event or _open_event(season, bootstrap, SELF_COHORT, 1, data_root=data_root)
            if resolved is not None:
                targets.append(self_target(entry_id, resolved))

    if cohort in {MINI_COHORT, "all"}:
        if league_id is None:
            if cohort == MINI_COHORT:
                raise typer.BadParameter(
                    f"no mini-league configured; pass --league or set ${MINI_LEAGUE_ENV_VAR}"
                )
            typer.secho(
                f"No mini-league configured (${MINI_LEAGUE_ENV_VAR} unset); skipping that cohort.",
                err=True,
                fg=typer.colors.YELLOW,
            )
        else:
            resolved = event or _open_event(season, bootstrap, MINI_COHORT, 1, data_root=data_root)
            if resolved is not None:
                targets.append(mini_target(league_id, resolved))

    if cohort in {ELITE_COHORT, "all"}:
        resolved = event or _open_event(
            season, bootstrap, ELITE_COHORT, ELITE_FIRST_EVENT, data_root=data_root
        )
        if resolved is not None:
            targets.append(elite_target(resolved, top))

    return targets


def _discover_league(season: Season, entry_id: int) -> int | None:
    """Find the mini-league from our own team rather than being told it.

    League IDs change every season and are not known until someone creates the
    league and we join it, so hand-configuring one guarantees a window where
    the job is silently capturing nothing. Reading it from our own entry closes
    that window without a person in the loop.

    Ambiguity is not resolved by guessing. With more than one private league we
    say which we found and capture none, because picking the wrong opponents is
    worse than capturing none and being told.
    """
    try:
        with FplApiConnector(season) as connector:
            leagues = discover_private_leagues(connector, entry_id)
    except SourceError as exc:
        typer.secho(f"Could not read leagues for entry {entry_id}: {exc}", err=True, fg="yellow")
        return None

    if not leagues:
        typer.secho(
            f"Entry {entry_id} is in no private league yet; skipping the mini cohort.",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return None
    if len(leagues) > 1:
        listed = ", ".join(f"{league.name} ({league.id})" for league in leagues)
        typer.secho(
            f"Entry {entry_id} is in several private leagues: {listed}. "
            f"Pass --league or set ${MINI_LEAGUE_ENV_VAR} to choose.",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return None

    found = leagues[0]
    typer.echo(f"Discovered mini-league {found.name} ({found.id}) from entry {entry_id}")
    return found.id


@app.command("discover-league")
def discover_league_command(
    season: SeasonOption = str(CURRENT_SEASON),
    entry: Annotated[
        int | None,
        typer.Option("--entry", help=f"Team ID. Defaults to ${ENTRY_ENV_VAR}."),
    ] = None,
) -> None:
    """List the private leagues a team belongs to, with their IDs."""
    parsed = _parse_season(season)
    entry_id = entry if entry is not None else Config.load().entry_id
    if entry_id is None:
        raise typer.BadParameter(f"no team configured; pass --entry or set ${ENTRY_ENV_VAR}")

    with _source_failures(), FplApiConnector(parsed) as connector:
        leagues = discover_private_leagues(connector, entry_id)

    if not leagues:
        typer.echo(f"Entry {entry_id} is in no private leagues yet.")
        return
    for league in leagues:
        typer.echo(f"{league.id}\t{league.name}")


def _open_event(
    season: Season,
    bootstrap: dict,
    cohort: str,
    first_event: int,
    *,
    data_root: Path | None,
) -> int | None:
    captured = {
        int(path.name.removeprefix("event="))
        for path in _captured_event_dirs(season, cohort, data_root)
    }
    return resolve_capture_event(bootstrap, datetime.now(UTC), captured, first_event=first_event)


def _captured_event_dirs(season: Season, cohort: str, data_root: Path | None):
    """Gameweeks already holding at least one complete chunk for this cohort."""
    parent = paths.raw_endpoint_dir(
        "fpl", "entry_picks", season, cohort=cohort, data_root=data_root
    )
    if not parent.is_dir():
        return []
    return [
        path
        for path in parent.iterdir()
        if path.is_dir()
        and path.name.startswith("event=")
        and any(chunk.is_dir() for chunk in path.iterdir())
    ]


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
