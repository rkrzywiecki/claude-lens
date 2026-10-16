"""Typer CLI: import, report."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from claude_lens.config import get_settings
from claude_lens.importers.claude_code import import_transcripts
from claude_lens.log_config import configure_logging
from claude_lens.models import GroupBy, ImportStats
from claude_lens.pricing import PricingError, load_pricing
from claude_lens.render import render_import, render_report
from claude_lens.report import build_report
from claude_lens.store import Store, StoreError

app = typer.Typer(
    help="Cost, cache and efficiency analytics for Claude.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


@app.callback()
def _main() -> None:
    configure_logging(get_settings().log_level)


def _open_store(db: Path | None) -> Store:
    try:
        return Store(db or get_settings().resolved_db_path())
    except (StoreError, OSError) as exc:
        err_console.print(f"[red]Cannot open database: {exc}[/red]")
        raise typer.Exit(2) from exc


def _run_import(store: Store, source: Path | None, full: bool) -> ImportStats:
    root = source or get_settings().resolved_transcripts_dir()
    if not root.is_dir():
        err_console.print(f"[red]Transcript directory not found: {root}[/red]")
        raise typer.Exit(2)
    return import_transcripts(store, root, full=full)


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time()).astimezone()


@app.command("import")
def import_cmd(
    source: Annotated[
        Path | None,
        typer.Option("--dir", help="Transcript root (default: LENS_TRANSCRIPTS_DIR)."),
    ] = None,
    full: Annotated[bool, typer.Option(help="Re-read files that did not change.")] = False,
    db: Annotated[Path | None, typer.Option(help="Database path (default: LENS_DB_PATH).")] = None,
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
    days: Annotated[
        int | None, typer.Option(min=1, help="Only the last N days (including today).")
    ] = None,
    since: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="First day (local).")
    ] = None,
    until: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Last day, inclusive (local).")
    ] = None,
    project: Annotated[
        str | None, typer.Option(help="Only projects whose path contains this text.")
    ] = None,
    top: Annotated[int | None, typer.Option(min=1, help="Show only the N largest rows.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
    refresh: Annotated[
        bool, typer.Option(help="Import changed transcripts before reporting.")
    ] = True,
    db: Annotated[Path | None, typer.Option(help="Database path (default: LENS_DB_PATH).")] = None,
) -> None:
    """Show API-equivalent cost, token usage and cache hit ratio."""
    if by not in ("day", "project", "model", "session"):
        err_console.print(f"[red]--by must be day, project, model or session, not {by!r}[/red]")
        raise typer.Exit(2)
    group_by: GroupBy = by  # type: ignore[assignment]
    if days is not None and since is not None:
        err_console.print("[red]Use either --days or --since, not both.[/red]")
        raise typer.Exit(2)
    start = _local_midnight(since.date()) if since else None
    if days is not None:
        start = _local_midnight(date.today() - timedelta(days=days - 1))
    end = _local_midnight(until.date() + timedelta(days=1)) if until else None
    try:
        pricing = load_pricing(get_settings().resolved_pricing_path())
    except PricingError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    with _open_store(db) as store:
        root = get_settings().resolved_transcripts_dir()
        if refresh and root.is_dir():
            import_transcripts(store, root)
        result = build_report(store.requests(start, end, project), pricing, group_by, top)
    if as_json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        render_report(console, result)
