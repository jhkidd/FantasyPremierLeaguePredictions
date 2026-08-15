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
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import polars as pl
import typer

from fpl import __version__, exit_codes, log
from fpl.clubelo_backfill import (
    backfill_clubelo_ratings,
    captured_dates,
    total_dates_in_scope,
)
from fpl.config import (
    CURRENT_SEASON,
    DEFAULT_ELITE_COHORT_SIZE,
    ENTRY_ENV_VAR,
    MINI_LEAGUE_ENV_VAR,
    Config,
    Season,
)
from fpl.facts.player_fixture import write_player_fixture_facts
from fpl.facts.points import write_points
from fpl.facts.team_fixture import write_team_fixture_facts
from fpl.features import library as features_library
from fpl.features.team_context import TEAM_CONTEXT_COLUMNS
from fpl.identity.players import (
    build_players_crosswalk,
    unmapped_players_with_minutes,
    validate_name_variants,
    write_players_crosswalk,
)
from fpl.identity.players_understat import (
    load_players_understat_crosswalk,
    unmapped_understat_players_with_minutes,
)
from fpl.identity.players_understat import (
    refresh_players_crosswalk as refresh_players_understat_crosswalk,
)
from fpl.identity.players_understat import (
    write_players_crosswalk as write_players_understat_crosswalk,
)
from fpl.identity.team_external_ids import (
    collect_source_names,
    load_team_external_ids,
    refresh_team_external_ids,
    unmapped_source_names,
    write_team_external_ids,
)
from fpl.identity.teams import build_teams_crosswalk, write_teams_crosswalk
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
from fpl.quality.checks import check_facts_tables, check_staged_tables
from fpl.quality.gates import has_blocking_violations
from fpl.scoring.base import POSITIONS
from fpl.sources.clubelo import ClubEloConnector
from fpl.sources.errors import BlockedError, SchemaError, SourceError
from fpl.sources.footballdata import FootballDataConnector
from fpl.sources.fpl_api import FplApiConnector
from fpl.sources.openfootball import OpenfootballConnector
from fpl.sources.understat import UnderstatConnector
from fpl.sources.vaastav import VaastavConnector
from fpl.staging.pipeline import (
    StageResult,
    stage_clubelo_source,
    stage_footballdata_source,
    stage_fpl_source,
    stage_openfootball_source,
    stage_understat_source,
    stage_vaastav_fixtures,
    stage_vaastav_source,
    stage_vaastav_teams,
)
from fpl.staging.vaastav import ERA_BY_SEASON
from fpl.storage import paths
from fpl.storage.parquet_io import read_parquet, write_parquet
from fpl.storage.raw_io import write_raw
from fpl.training.baseline import (
    GLM_COMPONENTS,
    fit_glm_baseline,
    naive_rolling_mean_predictions,
    predict_glm_baseline,
)
from fpl.training.baseline_report import render_baseline_report
from fpl.training.dataset import LABEL_COLUMNS, build_training_matrix
from fpl.training.eda import run_eda_sweep
from fpl.training.eda_plots import (
    plot_correlation_heatmap,
    plot_feature_histograms,
    plot_missingness_by_season,
    plot_target_distribution,
)
from fpl.training.eda_report import render_eda_report
from fpl.training.evaluation import (
    assemble_predicted_points,
    component_regression_metrics,
    points_error_report,
    spearman_by_gameweek,
)
from fpl.training.splits import VALIDATION_SEASON, chronological_split
from fpl.understat_capture import capture_league_data, capture_match_data

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


def _parse_as_of(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter(f"malformed --as-of {value!r}: {exc}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


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
    as_of_date: Annotated[
        str | None,
        typer.Option(
            "--as-of-date",
            help="Club Elo only: date to fetch ratings for (YYYY-MM-DD). Defaults to today.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Understat match_backfill only: cap the number of matches fetched this run.",
        ),
    ] = None,
) -> None:
    """Pull from a source into data/raw/."""
    parsed = _parse_season(season)

    if source == "vaastav":
        with _source_failures(), VaastavConnector() as connector:
            results = connector.fetch_and_store_season(
                parsed, force=force, data_root=_data_root(ctx)
            )
        written = sum(1 for result in results if result.written)
        typer.echo(
            f"{len(results)} file(s) pulled, {written} written, {len(results) - written} unchanged"
        )
        return

    if source == "openfootball":
        with _source_failures(), OpenfootballConnector() as connector:
            results = connector.fetch_and_store_season(
                parsed, force=force, data_root=_data_root(ctx)
            )
        written = sum(1 for result in results if result.written)
        typer.echo(
            f"{len(results)} file(s) pulled, {written} written, {len(results) - written} unchanged"
        )
        return

    if source == "footballdata":
        with _source_failures(), FootballDataConnector() as connector:
            body = connector.fetch_season(parsed)
            artifact = connector.artifact_for_season(body, parsed)
        result = write_raw(artifact, force=force, data_root=_data_root(ctx))
        typer.echo(f"1 endpoint(s) pulled, {1 if result.written else 0} written")
        return

    if source == "clubelo":
        parsed_date = date.fromisoformat(as_of_date) if as_of_date else datetime.now(UTC).date()
        with _source_failures(), ClubEloConnector() as connector:
            body = connector.fetch_ratings(parsed_date)
            artifact = connector.artifact_for_ratings(body, parsed_date, parsed)
        result = write_raw(artifact, force=force, data_root=_data_root(ctx))
        typer.echo(f"1 endpoint(s) pulled, {1 if result.written else 0} written")
        return

    if source == "understat":
        if endpoint == "match_backfill":
            with _source_failures(), UnderstatConnector() as connector:
                outcome = capture_match_data(
                    parsed, connector=connector, data_root=_data_root(ctx), limit=limit
                )
            typer.echo(
                f"{outcome.matches} match(es) in scope, {outcome.chunks_written} chunk(s) written, "
                f"{outcome.chunks_skipped} chunk(s) already captured"
            )
            return
        with _source_failures():
            result = capture_league_data(parsed, force=force, data_root=_data_root(ctx))
        typer.echo(f"1 endpoint(s) pulled, {1 if result.written else 0} written")
        return

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


def _stage_vaastav_calendar(
    season: Season,
    *,
    data_root: Path | None,
    tables: set[str] | None = None,
) -> list[StageResult]:
    """Stage the season's ``fixtures`` then ``teams``, in that order.

    Order matters and is not interchangeable: the two earliest seasons
    reconstruct ``fixtures`` from ``player_fixture_stats``, and ``teams`` for
    those same seasons is then derived from that fixture calendar. Staging
    them the other way round would silently produce nothing for 2016/17 and
    2017/18.
    """
    results: list[StageResult] = []
    if tables is None or "fixtures" in tables:
        results += stage_vaastav_fixtures(season, data_root=data_root)
    if tables is None or "teams" in tables:
        results += stage_vaastav_teams(season, data_root=data_root)
    return results


@app.command()
def stage(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Source name.")],
    season: SeasonOption = str(CURRENT_SEASON),
    table: Annotated[
        str | None,
        typer.Option("--table", help="Restrict to one staged table. Repeatable via commas."),
    ] = None,
) -> None:
    """Transform data/raw/ into typed tables in data/staged/."""
    parsed = _parse_season(season)
    if source not in {"fpl", "vaastav", "clubelo", "footballdata", "openfootball", "understat"}:
        _pending(4, f"stage {source}")

    tables = {t.strip() for t in table.split(",")} if table else None
    if source == "fpl":
        results = stage_fpl_source(parsed, data_root=_data_root(ctx), tables=tables)
    elif source == "clubelo":
        results = stage_clubelo_source(parsed, data_root=_data_root(ctx))
    elif source == "footballdata":
        results = stage_footballdata_source(parsed, data_root=_data_root(ctx))
    elif source == "openfootball":
        results = stage_openfootball_source(parsed, data_root=_data_root(ctx))
    elif source == "understat":
        results = stage_understat_source(parsed, data_root=_data_root(ctx))
    else:
        try:
            results = stage_vaastav_source(parsed, data_root=_data_root(ctx))
            results += _stage_vaastav_calendar(parsed, data_root=_data_root(ctx), tables=tables)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    for result in results:
        status = "staged" if result.written else "skipped"
        detail = f" ({result.detail})" if result.detail else ""
        typer.echo(f"{result.table}: {status}, {result.rows} row(s){detail}")
        if result.report and result.report.unknown_columns:
            typer.secho(
                f"  unknown column(s) seen and dropped: {list(result.report.unknown_columns)}",
                err=True,
                fg=typer.colors.YELLOW,
            )


@app.command()
def facts(
    ctx: typer.Context,
    season: SeasonOption = str(CURRENT_SEASON),
    rules: Annotated[
        str | None,
        typer.Option("--rules", help="Scoring ruleset: legacy, 2025-26, or 2026-27."),
    ] = None,
) -> None:
    """Assemble canonical player-fixture facts from data/staged/."""
    parsed = _parse_season(season)
    data_root = _data_root(ctx)

    facts_result = write_player_fixture_facts(parsed, data_root=data_root)
    if not facts_result.written:
        typer.echo(f"player_fixture: skipped, {facts_result.detail}")
    else:
        typer.echo(f"player_fixture: written, {facts_result.frame.height} row(s)")

    team_fixture_result = write_team_fixture_facts(parsed, data_root=data_root)
    if not team_fixture_result.written:
        typer.echo(f"team_fixture: skipped, {team_fixture_result.detail}")
    else:
        typer.echo(f"team_fixture: written, {team_fixture_result.frame.height} row(s)")
        if team_fixture_result.unresolved_teams:
            typer.secho(
                f"  unresolved team name(s): {team_fixture_result.unresolved_teams}",
                err=True,
                fg=typer.colors.YELLOW,
            )

    if rules is None or not facts_result.written:
        return
    try:
        points_result = write_points(parsed, rules, data_root=data_root)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not points_result.written:
        typer.echo(f"points[{rules}]: skipped, {points_result.detail}")
        return
    typer.echo(f"points[{rules}]: written, {points_result.frame.height} row(s)")


@crosswalk_app.command("refresh")
def crosswalk_refresh(ctx: typer.Context) -> None:
    """Rebuild both FPL-internal crosswalks from ingested raw data and write
    them, then draft any new ``team_external_ids`` rows.

    ``refresh`` (rather than ``validate`` alone) is needed for the two
    FPL-internal crosswalks because there is nothing committed yet for a
    person to review a diff against on the first run. ``team_external_ids``
    is different: it is a genuine draft-then-hand-review crosswalk (plan
    §7.12), so this command only ever *adds* a row for a ``team_code`` not
    yet present - an already-reviewed row is never touched."""
    data_root = _data_root(ctx)
    seasons = sorted(ERA_BY_SEASON)

    players_crosswalk = build_players_crosswalk(seasons, data_root=data_root)
    players_path = write_players_crosswalk(players_crosswalk, data_root=data_root)
    typer.echo(f"players_fpl: written, {players_crosswalk.height} code(s) -> {players_path}")

    teams_crosswalk = build_teams_crosswalk(seasons, data_root=data_root)
    teams_path = write_teams_crosswalk(teams_crosswalk, data_root=data_root)
    typer.echo(f"teams: written, {teams_crosswalk.height} row(s) -> {teams_path}")

    source_names = collect_source_names(seasons, data_root=data_root)
    team_external_ids = refresh_team_external_ids(
        teams_crosswalk,
        clubelo_names=source_names["clubelo_name"],
        understat_names=source_names["understat_name"],
        footballdata_names=source_names["footballdata_couk_name"],
        openfootball_names=source_names["openfootball_name"],
        data_root=data_root,
    )
    team_external_ids_path = write_team_external_ids(team_external_ids, data_root=data_root)
    typer.echo(
        f"team_external_ids: written, {team_external_ids.height} row(s) -> {team_external_ids_path}"
    )

    players_understat = refresh_players_understat_crosswalk(seasons, data_root=data_root)
    players_understat_path = write_players_understat_crosswalk(
        players_understat, data_root=data_root
    )
    typer.echo(
        f"players_fpl_understat: written, {players_understat.height} row(s) -> "
        f"{players_understat_path}"
    )


@crosswalk_app.command("validate")
def crosswalk_validate(ctx: typer.Context) -> None:
    """Fail if any player with minutes is unmapped, a code looks reused, or a
    Tier 2 source published a club with no ``team_external_ids`` row."""
    data_root = _data_root(ctx)
    seasons = sorted(ERA_BY_SEASON)

    crosswalk = build_players_crosswalk(seasons, data_root=data_root)
    if crosswalk.height == 0:
        typer.secho("no players_raw ingested for any season yet", err=True, fg=typer.colors.YELLOW)
        raise typer.Exit(exit_codes.QUALITY_GATE_FAILED)

    problems = False
    conflicts = validate_name_variants(crosswalk)
    for conflict in conflicts:
        problems = True
        typer.secho(
            f"[block] name-variant conflict: code {conflict.player_code} -> "
            f"{list(conflict.name_variants)}",
            fg=typer.colors.RED,
        )

    for season in seasons:
        unmapped = unmapped_players_with_minutes(season, crosswalk, data_root=data_root)
        if unmapped:
            problems = True
            typer.secho(
                f"[block] {season}: {len(unmapped)} player(s) with minutes have no "
                f"player_code mapping: {unmapped[:10]}",
                fg=typer.colors.RED,
            )

    team_external_ids = load_team_external_ids(data_root=data_root)
    source_names = collect_source_names(seasons, data_root=data_root)
    for column, key in (
        ("clubelo_name", "clubelo_name"),
        ("footballdata_couk_name", "footballdata_couk_name"),
        ("openfootball_name", "openfootball_name"),
    ):
        unmapped_names = unmapped_source_names(source_names[key], team_external_ids, column)
        if unmapped_names:
            problems = True
            typer.secho(
                f"[block] team_external_ids.{column}: {len(unmapped_names)} name(s) with "
                f"Tier 2 activity have no crosswalk row: {unmapped_names[:10]}",
                fg=typer.colors.RED,
            )

    if problems:
        raise typer.Exit(exit_codes.QUALITY_GATE_FAILED)
    typer.echo(f"crosswalk validate: clean ({crosswalk.height} player code(s))")


@crosswalk_app.command("validate-understat")
def crosswalk_validate_understat(ctx: typer.Context) -> None:
    """Fail if any Understat player who recorded minutes has no
    ``players_fpl_understat`` crosswalk row (plan §7.10)."""
    data_root = _data_root(ctx)
    seasons = sorted(ERA_BY_SEASON)

    players_understat = load_players_understat_crosswalk(data_root=data_root)

    problems = False
    for season in seasons:
        unmapped = unmapped_understat_players_with_minutes(
            season, players_understat, data_root=data_root
        )
        if unmapped:
            problems = True
            typer.secho(
                f"[block] {season}: {len(unmapped)} understat player(s) with minutes have "
                f"no players_fpl_understat mapping: {unmapped[:10]}",
                fg=typer.colors.RED,
            )

    if problems:
        raise typer.Exit(exit_codes.QUALITY_GATE_FAILED)
    typer.echo(f"crosswalk validate-understat: clean ({players_understat.height} row(s))")


@app.command()
def check(
    ctx: typer.Context,
    season: SeasonOption = str(CURRENT_SEASON),
    layer: Annotated[
        str, typer.Option("--layer", help="Which layer to gate: staged, facts, or both.")
    ] = "both",
) -> None:
    """Run every data quality gate against staged and/or facts tables for one season."""
    if layer not in ("staged", "facts", "both"):
        raise typer.BadParameter("--layer must be one of: staged, facts, both")
    parsed = _parse_season(season)
    data_root = _data_root(ctx)
    violations = []
    if layer in ("staged", "both"):
        violations.extend(check_staged_tables(parsed, data_root=data_root))
    if layer in ("facts", "both"):
        violations.extend(check_facts_tables(parsed, data_root=data_root))
    if not violations:
        typer.echo("check: clean")
        raise typer.Exit(exit_codes.SUCCESS)

    for violation in violations:
        colour = typer.colors.RED if violation.severity == "block" else typer.colors.YELLOW
        typer.secho(f"[{violation.severity}] {violation.gate}: {violation.detail}", fg=colour)
        for row in violation.sample:
            typer.echo(f"    {row}")

    if has_blocking_violations(violations):
        raise typer.Exit(exit_codes.QUALITY_GATE_FAILED)
    raise typer.Exit(exit_codes.SUCCESS)


@app.command()
def features(
    ctx: typer.Context,
    season: SeasonOption = str(CURRENT_SEASON),
    as_of: Annotated[
        str, typer.Option("--as-of", help="ISO 8601 instant, UTC. Defaults to now.")
    ] = "",
    horizon_gameweeks: Annotated[
        int, typer.Option("--horizon-gameweeks", help="Gameweeks ahead.")
    ] = 1,
) -> None:
    """Build features at a point in time. Debug snapshot only — writes to
    data/features/, never the source of truth (that is features.library.build)."""
    parsed_season = _parse_season(season)
    moment = _parse_as_of(as_of) if as_of else datetime.now(UTC)
    data_root = _data_root(ctx)

    result = features_library.build(
        parsed_season, moment, horizon_gameweeks=horizon_gameweeks, data_root=data_root
    )
    if result.frame is None:
        typer.secho(f"features: skipped, {result.detail}", err=True, fg=typer.colors.RED)
        raise typer.Exit(exit_codes.FAILURE)

    out_dir = paths.data_features_table(parsed_season, moment, data_root=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(result.frame, out_dir / "part.parquet")

    typer.echo(f"features: {result.frame.height} row(s) written to {out_dir / 'part.parquet'}")
    typer.echo(
        f"features: team resolution fell back to current team for "
        f"{result.diagnostics.fallback_count} player(s)"
    )


@app.command()
def dataset(ctx: typer.Context) -> None:
    """Build the training matrix across every season with built
    ``facts/player_fixture`` and write it to ``data/training/matrix.parquet``.

    A season is silently skipped (not an error) when its facts have not been
    built yet — the same "missing is a normal, expected state" contract
    every other ``facts``-reading command in this CLI already follows."""
    data_root = _data_root(ctx)
    seasons = _seasons_with_built_facts(data_root)
    if not seasons:
        typer.echo("dataset: skipped, no facts/player_fixture built for any season yet")
        return

    matrix = build_training_matrix(seasons, data_root=data_root)

    out_path = paths.data_training_matrix(data_root=data_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_parquet(matrix, out_path)

    typer.echo(
        f"dataset: {matrix.height} row(s) across {len(seasons)} season(s) written to {out_path}"
    )


_DEFAULT_EDA_REPORT_PATH = Path(__file__).resolve().parents[2] / "docs" / "model-prototype-eda.md"


def _seasons_with_built_facts(data_root: Path | None) -> list[Season]:
    """Every season with a built ``facts/player_fixture`` table, in order —
    the shared "what can we train on right now" contract for ``dataset``,
    ``eda`` and (later) ``baseline``."""
    return [
        season
        for season in sorted(ERA_BY_SEASON)
        if (
            paths.facts_table("player_fixture", season, data_root=data_root) / "part.parquet"
        ).exists()
    ]


def _load_or_build_training_matrix(data_root: Path | None) -> pl.DataFrame | None:
    """Load the cached ``data/training/matrix.parquet`` if ``fpl dataset``
    has already written one, otherwise build it in memory from every season
    with built facts (without writing it to disk). Returns ``None`` if there
    is nothing to build from at all."""
    matrix_path = paths.data_training_matrix(data_root=data_root)
    if matrix_path.exists():
        return read_parquet(matrix_path)
    seasons = _seasons_with_built_facts(data_root)
    if not seasons:
        return None
    return build_training_matrix(seasons, data_root=data_root)


def _curated_eda_columns(frame: pl.DataFrame) -> list[str]:
    """A tractable, representative numeric-feature subset for VIF and
    plotting: the deepest fixed rolling window's per-90 rate — one
    representation per underlying stat, rather than every window x
    sum/per90 combination — plus every team-context column present. A full
    VIF or one histogram per one of ~470 mutually-derived columns would be
    both computationally infeasible and unreadable."""
    rolling = sorted(c for c in frame.columns if c.endswith("_per90_last_10"))
    team_context = [c for c in TEAM_CONTEXT_COLUMNS if c in frame.columns]
    return rolling + team_context


@app.command()
def eda(
    ctx: typer.Context,
    report_path: Annotated[
        Path | None,
        typer.Option(
            "--report-path", help="Override the markdown report output path (for testing)."
        ),
    ] = None,
) -> None:
    """Run the Step 25-26 EDA statistical sweep and plots over the
    chronological training split **only**, and write the figures to
    ``data/eda/`` plus a markdown report to ``docs/model-prototype-eda.md``.

    Builds/loads the training matrix the same way ``fpl dataset`` does if
    ``data/training/matrix.parquet`` is not already present."""
    data_root = _data_root(ctx)
    matrix = _load_or_build_training_matrix(data_root)
    if matrix is None:
        typer.echo("eda: skipped, no training matrix available yet (run `fpl dataset` first)")
        return

    train, _validation, _test = chronological_split(matrix)
    if train.height == 0:
        typer.echo("eda: skipped, chronological training split is empty")
        return

    curated_columns = _curated_eda_columns(train)
    result = run_eda_sweep(train, vif_columns=curated_columns)

    eda_dir = paths.data_eda_dir(data_root=data_root)
    eda_dir.mkdir(parents=True, exist_ok=True)

    histogram_paths = plot_feature_histograms(train, curated_columns, eda_dir)
    target_distribution_paths = {
        label: plot_target_distribution(train, label, eda_dir) for label in LABEL_COLUMNS
    }
    correlation_heatmap_paths = {
        "pearson": plot_correlation_heatmap(result.pearson, eda_dir, name="pearson"),
        "spearman": plot_correlation_heatmap(result.spearman, eda_dir, name="spearman"),
    }
    missingness_path = plot_missingness_by_season(train, curated_columns, eda_dir)

    resolved_report_path = report_path or _DEFAULT_EDA_REPORT_PATH
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = render_eda_report(
        result,
        train_row_count=train.height,
        train_seasons=sorted(train["season"].unique().to_list()),
        curated_columns=curated_columns,
        histogram_paths=histogram_paths,
        target_distribution_paths=target_distribution_paths,
        correlation_heatmap_paths=correlation_heatmap_paths,
        missingness_path=missingness_path,
        report_path=resolved_report_path,
    )
    resolved_report_path.write_text(markdown, encoding="utf-8")

    typer.echo(
        f"eda: {train.height} training row(s) analysed, report written to "
        f"{resolved_report_path}, figures written to {eda_dir}"
    )


_DEFAULT_BASELINE_REPORT_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "model-prototype-baseline.md"
)

# Every LABEL_COLUMNS entry with its "label_" prefix stripped - the naive
# baseline's own target-name convention (fpl.training.baseline).
_ALL_TARGETS: tuple[str, ...] = tuple(label[len("label_") :] for label in LABEL_COLUMNS)

# glm_minutes plus every GLM_COMPONENTS target - the components the GLM
# per-position metrics table covers. Poisson deviance only applies to the
# count targets, never to minutes (Ridge).
_GLM_TARGETS: tuple[str, ...] = ("minutes", *GLM_COMPONENTS)


def _naive_metrics_table(validation: pl.DataFrame) -> pl.DataFrame:
    rows = [
        {
            "component": target,
            **component_regression_metrics(
                validation, actual_column=f"label_{target}", predicted_column=f"naive_{target}"
            ),
        }
        for target in _ALL_TARGETS
    ]
    return pl.DataFrame(rows)


def _glm_metrics_table(validation: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for target in _GLM_TARGETS:
        is_count_target = target in GLM_COMPONENTS
        for position in sorted(POSITIONS):
            subset = validation.filter(pl.col("position") == position)
            metrics = component_regression_metrics(
                subset,
                actual_column=f"label_{target}",
                predicted_column=f"glm_{target}",
                poisson=is_count_target,
            )
            rows.append({"component": target, "position": position, **metrics})
    return pl.DataFrame(rows)


@app.command()
def baseline(
    ctx: typer.Context,
    report_path: Annotated[
        Path | None,
        typer.Option(
            "--report-path", help="Override the markdown report output path (for testing)."
        ),
    ] = None,
) -> None:
    """Fit the Step 28 naive and Step 29 GLM baselines on the chronological
    training split, evaluate both **on the validation split only**, and
    write a markdown report to ``docs/model-prototype-baseline.md``.

    Never reads the test split (plan Step 31's explicit boundary) - that
    one-time final read is reserved for later in Phase A/B.

    Builds/loads the training matrix the same way ``fpl dataset``/``fpl eda``
    do if ``data/training/matrix.parquet`` is not already present."""
    data_root = _data_root(ctx)
    matrix = _load_or_build_training_matrix(data_root)
    if matrix is None:
        typer.echo("baseline: skipped, no training matrix available yet (run `fpl dataset` first)")
        return

    train, validation, _test = chronological_split(matrix)
    if train.height == 0 or validation.height == 0:
        typer.echo("baseline: skipped, chronological train/validation split is empty")
        return

    glm_bundle = fit_glm_baseline(train)
    validation_with_glm = predict_glm_baseline(glm_bundle, validation)
    validation_with_predictions = naive_rolling_mean_predictions(validation_with_glm)

    scored = assemble_predicted_points(validation_with_predictions)

    markdown = render_baseline_report(
        train_row_count=train.height,
        train_seasons=sorted(train["season"].unique().to_list()),
        validation_row_count=validation.height,
        validation_season=VALIDATION_SEASON,
        naive_metrics=_naive_metrics_table(validation_with_predictions),
        glm_metrics=_glm_metrics_table(validation_with_predictions),
        points_report=points_error_report(scored),
        gameweek_spearman=spearman_by_gameweek(scored),
        report_path=report_path or _DEFAULT_BASELINE_REPORT_PATH,
    )

    resolved_report_path = report_path or _DEFAULT_BASELINE_REPORT_PATH
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_report_path.write_text(markdown, encoding="utf-8")

    typer.echo(
        f"baseline: {validation.height} validation row(s) evaluated, report written to "
        f"{resolved_report_path}"
    )


def _rules_for_season(season: Season) -> str:
    """The scoring ruleset each backfilled season reconciles against.

    2025/26 is the only season carrying the defensive-contribution term;
    every earlier season uses the legacy ruleset (spec §15)."""
    return "2025-26" if season == Season(2025) else "legacy"


@app.command("backfill-elo")
def backfill_elo(
    ctx: typer.Context,
    from_season: Annotated[str, typer.Option("--from")] = "2016-17",
    to_season: Annotated[str, typer.Option("--to")] = "2025-26",
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Stop after this many fetches. Useful for a smoke test."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report how many dates would be fetched, then stop."),
    ] = False,
) -> None:
    """Fetch historical Club Elo ratings for every matchday, then stage them.

    Elo is a point-in-time rating that today's endpoint cannot reproduce for a
    past date, so a decade of ratings needs a decade of requests — roughly
    1,153 across ten seasons at ~7s each. The run is resumable and safe to
    re-run: already-captured dates are skipped by reading each partition's
    recorded rating date, and a failure on one date does not abort the rest.

    Requires ``facts/player_fixture`` to exist, since the date list is derived
    from the fixtures that actually need rating.
    """
    start = _parse_season(from_season)
    end = _parse_season(to_season)
    seasons = [s for s in sorted(ERA_BY_SEASON) if start <= s <= end]
    if not seasons:
        raise typer.BadParameter("no classified season falls within --from/--to")

    data_root = _data_root(ctx)

    if dry_run:
        counts = total_dates_in_scope(seasons, data_root=data_root)
        total = 0
        for season in seasons:
            already = len(captured_dates(season, data_root=data_root))
            outstanding = max(counts[season] - already, 0)
            total += outstanding
            typer.echo(
                f"{season}: {counts[season]} date(s) in scope, {already} captured, "
                f"{outstanding} to fetch"
            )
        typer.echo(f"total: {total} date(s) to fetch, roughly {total * 7 // 60} minute(s)")
        return

    def _progress(season: Season, day: date, index: int, total: int) -> None:
        typer.echo(f"{season}: fetching {day} ({index}/{total})")

    with _source_failures():
        outcomes = backfill_clubelo_ratings(
            seasons, data_root=data_root, limit=limit, progress=_progress
        )

    failures = 0
    for outcome in outcomes:
        typer.echo(
            f"{outcome.season}: {outcome.fetched} fetched, {outcome.skipped} already captured, "
            f"{len(outcome.failed)} failed"
        )
        for day, detail in outcome.failed:
            typer.secho(f"  {day}: {detail}", err=True, fg=typer.colors.YELLOW)
        failures += len(outcome.failed)

        for result in stage_clubelo_source(outcome.season, data_root=data_root):
            typer.echo(f"{outcome.season} stage[{result.table}]: {result.rows} row(s)")

    if failures:
        typer.secho(
            f"{failures} date(s) failed; re-run to retry exactly those.",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(exit_codes.FAILURE)


@app.command()
def backfill(
    ctx: typer.Context,
    from_season: Annotated[str, typer.Option("--from")] = "2016-17",
    to_season: Annotated[str, typer.Option("--to")] = "2025-26",
    skip_fetch: Annotated[
        bool,
        typer.Option("--skip-fetch", help="Re-derive from raw already on disk; no network."),
    ] = False,
) -> None:
    """One-off historical cold start: fetch, stage, facts and check every season.

    Fails loudly on the first season that will not reconcile rather than
    pressing on — a partial backfill that looks complete is the failure
    mode the quality gates exist to prevent."""
    start = _parse_season(from_season)
    end = _parse_season(to_season)
    seasons = [s for s in sorted(ERA_BY_SEASON) if start <= s <= end]
    if not seasons:
        raise typer.BadParameter("no classified season falls within --from/--to")

    data_root = _data_root(ctx)

    if not skip_fetch:
        with _source_failures(), VaastavConnector() as connector:
            for season in seasons:
                results = connector.fetch_and_store_season(season, data_root=data_root)
                written = sum(1 for result in results if result.written)
                typer.echo(f"{season}: fetched, {written}/{len(results)} file(s) written")

    for season in seasons:
        try:
            stage_results = stage_vaastav_source(season, data_root=data_root)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        for result in stage_results:
            typer.echo(f"{season} stage[{result.table}]: {result.rows} row(s)")

        facts_result = write_player_fixture_facts(season, data_root=data_root)
        if not facts_result.written:
            typer.secho(
                f"{season}: player_fixture facts skipped, {facts_result.detail}",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(exit_codes.FAILURE)

        # After player_fixture_stats, because 2016/17-2017/18 reconstruct
        # their fixture calendar from it, and after that because those same
        # seasons derive their teams table from the calendar.
        for result in _stage_vaastav_calendar(season, data_root=data_root):
            typer.echo(f"{season} stage[{result.table}]: {result.rows} row(s)")

        rules = _rules_for_season(season)
        points_result = write_points(season, rules, data_root=data_root)
        typer.echo(
            f"{season}: facts {facts_result.frame.height} row(s), "
            f"points[{rules}] {points_result.frame.height} row(s)"
        )

        violations = check_staged_tables(season, data_root=data_root)
        violations.extend(check_facts_tables(season, data_root=data_root))
        if has_blocking_violations(violations):
            for violation in violations:
                if violation.severity == "block":
                    typer.secho(
                        f"[block] {season} {violation.gate}: {violation.detail}",
                        fg=typer.colors.RED,
                    )
            typer.secho(
                f"{season}: did not reconcile; stopping backfill here",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(exit_codes.QUALITY_GATE_FAILED)

    typer.echo(f"backfill: {len(seasons)} season(s) reconciled, {seasons[0]}..{seasons[-1]}")
