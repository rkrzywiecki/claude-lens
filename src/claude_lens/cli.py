"""Typer CLI: import, report, cache-doctor, loops, session, serve."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel
from rich.console import Console

from claude_lens import queries
from claude_lens.config import get_settings, is_loopback
from claude_lens.importers.claude_code import import_transcripts
from claude_lens.log_config import configure_logging
from claude_lens.models import GroupBy, ImportStats
from claude_lens.pricing import Pricing, PricingError, load_pricing
from claude_lens.queries import Filters, QueryError
from claude_lens.render import (
    render_cache_doctor,
    render_import,
    render_loops,
    render_report,
    render_session,
)
from claude_lens.store import Store, StoreError

app = typer.Typer(
    help="Cost, cache and efficiency analytics for Claude.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)

# Options shared by the analysis commands.
DaysOpt = Annotated[int | None, typer.Option(min=1, help="Only the last N days (including today).")]
SinceOpt = Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], help="First day (local).")]
UntilOpt = Annotated[
    datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Last day, inclusive (local).")
]
ProjectOpt = Annotated[
    str | None, typer.Option(help="Only projects whose path contains this text.")
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print the result as JSON.")]
RefreshOpt = Annotated[bool, typer.Option(help="Import changed transcripts first.")]
DbOpt = Annotated[Path | None, typer.Option(help="Database path (default: LENS_DB_PATH).")]
HostOpt = Annotated[str | None, typer.Option(help="Listen address (default: LENS_HOST).")]
AllowNonLoopbackOpt = Annotated[
    bool,
    typer.Option(
        "--allow-non-loopback",
        help="Allow listening on a non-loopback address (plain HTTP, no authentication).",
    ),
]


@app.callback()
def _main() -> None:
    configure_logging(get_settings().log_level)


def _fail(message: str) -> typer.Exit:
    err_console.print(f"[red]{message}[/red]")
    return typer.Exit(2)


def _open_store(db: Path | None) -> Store:
    try:
        return Store(db or get_settings().resolved_db_path())
    except (StoreError, OSError) as exc:
        raise _fail(f"Cannot open database: {exc}") from exc


def _pricing() -> Pricing:
    try:
        return load_pricing(get_settings().resolved_pricing_path())
    except PricingError as exc:
        raise _fail(str(exc)) from exc


def _filters(
    days: int | None, since: datetime | None, until: datetime | None, project: str | None
) -> Filters:
    try:
        return Filters.from_days(
            days, since.date() if since else None, until.date() if until else None, project
        )
    except QueryError as exc:
        raise _fail(f"{exc}.") from exc


def _run_import(store: Store, source: Path | None, full: bool) -> ImportStats:
    root = source or get_settings().resolved_transcripts_dir()
    if not root.is_dir():
        raise _fail(f"Transcript directory not found: {root}")
    return import_transcripts(store, root, full=full)


def _refresh(store: Store, refresh: bool) -> None:
    """Quietly import changed transcripts; skipped when the directory does not exist."""
    root = get_settings().resolved_transcripts_dir()
    if refresh and root.is_dir():
        import_transcripts(store, root)


def _check_bind(host: str, allow: bool, what: str) -> None:
    if not is_loopback(host) and not (allow or get_settings().allow_non_loopback):
        raise _fail(
            f"Refusing to listen on non-loopback address {host!r}: {what} would be reachable "
            "from the network as plain HTTP. Bind to 127.0.0.1, or pass --allow-non-loopback "
            "(LENS_ALLOW_NON_LOOPBACK=true) if TLS and access control sit in front."
        )


def _echo_json(model: BaseModel) -> None:
    typer.echo(model.model_dump_json(indent=2))


@app.command("import")
def import_cmd(
    source: Annotated[
        Path | None,
        typer.Option("--dir", help="Transcript root (default: LENS_TRANSCRIPTS_DIR)."),
    ] = None,
    full: Annotated[bool, typer.Option(help="Re-read files that did not change.")] = False,
    db: DbOpt = None,
) -> None:
    """Import Claude Code transcripts into the local database."""
    with _open_store(db) as store:
        stats = _run_import(store, source, full)
        render_import(console, stats, store.count())


@app.command()
def report(
    by: Annotated[str, typer.Option("--by", help="Group by: day, project, model, session.")] = (
        "day"
    ),
    days: DaysOpt = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
    project: ProjectOpt = None,
    top: Annotated[int | None, typer.Option(min=1, help="Show only the N largest rows.")] = None,
    as_json: JsonOpt = False,
    refresh: RefreshOpt = True,
    db: DbOpt = None,
) -> None:
    """Show API-equivalent cost, token usage and cache hit ratio."""
    if by not in ("day", "project", "model", "session"):
        raise _fail(f"--by must be day, project, model or session, not {by!r}")
    group_by: GroupBy = by  # type: ignore[assignment]
    filters = _filters(days, since, until, project)
    pricing = _pricing()
    with _open_store(db) as store:
        _refresh(store, refresh)
        result = queries.report(store, pricing, group_by, filters, top)
    if as_json:
        _echo_json(result)
    else:
        render_report(console, result)


@app.command("cache-doctor")
def cache_doctor(
    days: DaysOpt = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
    project: ProjectOpt = None,
    top: Annotated[int, typer.Option(min=1, help="Sessions to list.")] = 10,
    as_json: JsonOpt = False,
    refresh: RefreshOpt = True,
    db: DbOpt = None,
) -> None:
    """Find prompt-cache misses, their cause and what they cost."""
    filters = _filters(days, since, until, project)
    pricing = _pricing()
    with _open_store(db) as store:
        _refresh(store, refresh)
        result = queries.cache_doctor(store, pricing, filters, top)
    if as_json:
        _echo_json(result)
    else:
        render_cache_doctor(console, result)


@app.command()
def loops(
    days: DaysOpt = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
    project: ProjectOpt = None,
    as_json: JsonOpt = False,
    refresh: RefreshOpt = True,
    db: DbOpt = None,
) -> None:
    """Find repeated tool calls, error streaks and files edited over and over."""
    filters = _filters(days, since, until, project)
    with _open_store(db) as store:
        _refresh(store, refresh)
        result = queries.loops(store, filters)
    if as_json:
        _echo_json(result)
    else:
        render_loops(console, result)


@app.command()
def session(
    session_id: Annotated[str, typer.Argument(help="Session id or a unique prefix of one.")],
    as_json: JsonOpt = False,
    refresh: RefreshOpt = True,
    db: DbOpt = None,
) -> None:
    """Show one session: request timeline, context growth, cache misses and loops."""
    pricing = _pricing()
    with _open_store(db) as store:
        _refresh(store, refresh)
        try:
            result = queries.session(store, pricing, session_id)
        except QueryError as exc:
            raise _fail(f"{exc}.") from exc
    if as_json:
        _echo_json(result)
    else:
        render_session(console, result)


@app.command()
def serve(
    host: HostOpt = None,
    port: Annotated[int | None, typer.Option(help="Listen port (default: LENS_PORT).")] = None,
    allow_non_loopback: AllowNonLoopbackOpt = False,
) -> None:
    """Run the recording pass-through proxy (point ANTHROPIC_BASE_URL at it)."""
    import uvicorn

    from claude_lens.proxy.app import create_app

    settings = get_settings()
    bind_host = host or settings.host
    bind_port = port or settings.port
    _check_bind(bind_host, allow_non_loopback, "API keys and prompts")
    shown = f"[{bind_host}]" if ":" in bind_host else bind_host
    err_console.print(
        f"claude-lens proxy on http://{shown}:{bind_port} -> {settings.upstream_url}, "
        f"recording to {settings.resolved_db_path()}"
    )
    uvicorn.run(
        create_app(settings),
        host=bind_host,
        port=bind_port,
        log_level=settings.log_level.lower(),
    )

