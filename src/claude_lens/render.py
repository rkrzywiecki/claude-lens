"""Terminal output (rich) for import and report."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from claude_lens.models import ImportStats, Report, ReportRow

_GROUP_TITLES = {"day": "Day", "project": "Project", "model": "Model", "session": "Session"}


def render_import(console: Console, stats: ImportStats, total_records: int) -> None:
    console.print(
        f"Imported {stats.files_imported} of {stats.files_seen} transcript files "
        f"({stats.files_skipped} unchanged or unreadable), {stats.records} responses; "
        f"{total_records} responses in the database."
    )
    if stats.bad_lines:
        console.print(f"[yellow]Skipped {stats.bad_lines} malformed lines.[/yellow]")


def render_report(console: Console, report: Report) -> None:
    if not report.rows:
        console.print("No requests in the selected range. Run `claude-lens import` first?")
        return
    table = Table(title=f"API-equivalent cost by {report.group_by}", title_justify="left")
    table.add_column(_GROUP_TITLES[report.group_by], overflow="fold")
    table.add_column("Requests", justify="right")
    table.add_column("Input", justify="right")
    table.add_column("Cache write", justify="right")
    table.add_column("Cache read", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Hit ratio", justify="right")
    table.add_column("Saved", justify="right")
    table.add_column("Cost", justify="right", style="bold")
    for row in report.rows:
        table.add_row(*_cells(row, _row_name(row, report)))
    table.add_section()
    table.add_row(*_cells(report.total, "Total"), style="bold")
    console.print(table)
    if report.unpriced_models:
        console.print(
            f"[yellow]No price for {', '.join(report.unpriced_models)} "
            f"({report.total.unpriced_requests} requests left out of costs). "
            "Add them to a pricing.yaml and set LENS_PRICING_PATH.[/yellow]"
        )


def _row_name(row: ReportRow, report: Report) -> str:
    if report.group_by == "session" and row.label:
        return f"{row.key[:8]}  {row.label}"
    return row.key


def _cells(row: ReportRow, name: str) -> list[str]:
    u = row.usage
    ratio = row.cache_hit_ratio
    cost = money(row.cost.total)
    if row.unpriced_requests:
        cost += "*"
    return [
        name,
        f"{row.requests:,}",
        tokens(u.input_tokens),
        tokens(u.cache_write_tokens),
        tokens(u.cache_read_tokens),
        tokens(u.output_tokens),
        "-" if ratio is None else f"{ratio:.0%}",
        money(row.cost.cache_savings),
        cost,
    ]


def money(value: float) -> str:
    """USD with the sign in front: $1.40, -$0.19."""
    return f"{'-' if value < -0.005 else ''}${abs(value):,.2f}"


def tokens(n: int) -> str:
    """Compact token count: 950, 12.3k, 4.5M, 1.2B."""
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= limit:
            return f"{n / limit:.1f}{suffix}"
    return str(n)
