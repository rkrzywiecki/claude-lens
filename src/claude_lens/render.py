"""Terminal output (rich) for the CLI commands."""

from __future__ import annotations

from datetime import datetime, timedelta

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from claude_lens.analysis.cache import CacheDoctorReport
from claude_lens.analysis.session import SessionDetail
from claude_lens.insights.coach import CoachResult
from claude_lens.insights.models import (
    Comparison,
    InsightsReport,
    MetricChange,
    TrendsReport,
    WeekMetrics,
)
from claude_lens.insights.trends import changes
from claude_lens.insights.whatif import WhatIf
from claude_lens.models import (
    ImportStats,
    OutcomeReport,
    OutcomeRow,
    Report,
    ReportRow,
    SessionOutcome,
)
from claude_lens.queries import LoopReport
from claude_lens.report import shorten_home

_GROUP_TITLES = {
    "day": "Day",
    "project": "Project",
    "model": "Model",
    "effort": "Effort",
    "session": "Session",
}


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


def render_outcome_report(console: Console, report: OutcomeReport) -> None:
    if not report.rows:
        console.print(
            f"No sessions in a local git repository ({report.untracked_sessions} sessions "
            "outside git). The project directory must still exist; see docs/outcomes.md."
        )
        return
    by = "main model" if report.group_by == "model" else report.group_by
    table = Table(title=f"Cost and git outcome by {by}", title_justify="left")
    table.add_column(_GROUP_TITLES[report.group_by], overflow="fold")
    table.add_column("Sessions", justify="right")
    table.add_column("Commits", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("+/- lines", justify="right")
    table.add_column("Edits, no commit", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Per commit", justify="right", style="bold")
    table.add_column("Per 100 lines", justify="right")
    for row in report.rows:
        name = (
            f"{row.key[:8]}  {row.label}" if report.group_by == "session" and row.label else row.key
        )
        table.add_row(*_outcome_cells(row, name))
    table.add_section()
    table.add_row(*_outcome_cells(report.total, "Total"), style="bold")
    console.print(table)
    left_out = (
        f"{report.untracked_sessions} sessions outside a git repository "
        f"({money(report.untracked_cost)}) are left out. "
        if report.untracked_sessions
        else ""
    )
    console.print(
        f"[dim]{left_out}Commits made during a session or "
        "30 minutes after it are a rough proxy for kept work, not for quality; differences "
        "between models or effort levels are correlations.[/dim]"
    )


def _outcome_cells(row: OutcomeRow, name: str) -> list[str]:
    return [
        name,
        f"{row.sessions:,}",
        f"{row.commits:,}",
        f"{row.files_changed:,}",
        f"+{row.lines_added:,} / -{row.lines_removed:,}",
        f"{row.edits_no_commit:,}",
        money(row.cost),
        "-" if row.cost_per_commit is None else money(row.cost_per_commit),
        "-" if row.cost_per_100_lines is None else money(row.cost_per_100_lines),
    ]


def outcome_line(outcome: SessionOutcome | None) -> str:
    if outcome is None:
        return "Git: not in a local repository (no outcome)"
    return (
        f"Git: {outcome.commits} commits, {outcome.files_changed} files, "
        f"+{outcome.lines_added:,} / -{outcome.lines_removed:,} lines"
    )


def money(value: float) -> str:
    """USD with the sign in front: $1.40, -$0.19."""
    return f"{'-' if value < -0.005 else ''}${abs(value):,.2f}"


def tokens(n: int) -> str:
    """Compact token count: 950, 12.3k, 4.5M, 1.2B."""
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= limit:
            return f"{n / limit:.1f}{suffix}"
    return str(n)


def _local(value: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.astimezone().strftime(fmt)


def _short(session_id: str) -> str:
    return session_id[:8]


_CAUSE_LABELS = {
    "prefix_changed": "prefix changed",
    "ttl_expired": "TTL expired",
    "unexplained": "unexplained",
    "context_reset": "context reset",
}


def render_cache_doctor(console: Console, report: CacheDoctorReport) -> None:
    if not report.sessions:
        console.print("No requests in the selected range. Run `claude-lens import` first?")
        return
    table = Table(title="Prompt-cache misses by session", title_justify="left")
    table.add_column("Session")
    table.add_column("Started")
    table.add_column("Project", overflow="fold")
    table.add_column("Requests", justify="right")
    table.add_column("Hit ratio", justify="right")
    table.add_column("Misses")
    table.add_column("Lost", justify="right")
    table.add_column("Avoidable", justify="right", style="bold")
    for s in report.sessions:
        ratio = "-" if s.hit_ratio is None else f"{s.hit_ratio:.0%}"
        causes = ", ".join(f"{n} {_CAUSE_LABELS[c]}" for c, n in s.counts.items() if n)
        table.add_row(
            _short(s.session_id),
            _local(s.started),
            shorten_home(s.project),
            f"{s.requests:,}",
            ratio,
            causes or "-",
            money(s.penalty),
            money(s.avoidable_penalty),
        )
    console.print(table)
    console.print(
        f"Listed sessions: {money(report.penalty)} lost to cache misses, "
        f"[bold]{money(report.avoidable_penalty)} avoidable[/bold]."
    )
    misses = [m for s in report.sessions for m in s.misses if m.cause != "context_reset"]
    if misses:
        worst = sorted(misses, key=lambda m: -m.penalty)[:5]
        console.print("\nCostliest misses:")
        for m in worst:
            console.print(
                f"  {_local(m.timestamp)}  {m.model}  {tokens(m.lost_tokens)} tokens re-sent, "
                f"{money(m.penalty)}: {_CAUSE_LABELS[m.cause]} ({m.detail})"
                + ("" if m.avoidable else " [dim]unavoidable[/dim]")
            )
    if report.advice:
        console.print("\nAdvice:")
        for line in report.advice:
            console.print(f"  - {line}")
    elif not misses:
        console.print("No avoidable cache misses found.")


_LOOP_LABELS = {
    "repeated_call": "same call repeated",
    "error_streak": "errors in a row",
    "file_churn": "file edited repeatedly",
}


def render_loops(console: Console, report: LoopReport) -> None:
    if not report.findings:
        console.print("No loops found.")
        return
    table = Table(title="Possible agent loops", title_justify="left")
    table.add_column("Session")
    table.add_column("Pattern")
    table.add_column("Tools")
    table.add_column("Count", justify="right", style="bold")
    table.add_column("When")
    table.add_column("Target", overflow="fold")
    for f in report.findings:
        project = report.projects.get(f.session_id)
        table.add_row(
            _short(f.session_id) + (f"  {shorten_home(project)}" if project else ""),
            _LOOP_LABELS[f.kind],
            ", ".join(f.tool_names),
            str(f.count),
            f"{_local(f.first)} - {_local(f.last, '%H:%M')}",
            shorten_home(f.target) if f.target else "",
        )
    console.print(table)


def render_session(console: Console, detail: SessionDetail) -> None:
    ctx = detail.context
    peak_share = f" ({ctx.peak_share:.0%} of the window)" if ctx.peak_share is not None else ""
    console.print(
        f"[bold]Session {detail.session_id}[/bold]  {shorten_home(detail.project)}\n"
        f"{_local(detail.started)} - {_local(detail.ended)}, {len(detail.timeline)} requests, "
        f"{money(detail.cost)}, cache hit ratio "
        + ("-" if detail.cache.hit_ratio is None else f"{detail.cache.hit_ratio:.0%}")
        + f"\nContext: peak {tokens(ctx.peak_prompt_tokens)} tokens{peak_share}, "
        f"last {tokens(ctx.final_prompt_tokens)}, {ctx.average_growth:+,.0f} tokens per request, "
        f"{ctx.resets} resets\n" + outcome_line(detail.outcome)
    )
    table = Table(title_justify="left")
    table.add_column("Time")
    table.add_column("Model")
    table.add_column("Prompt", justify="right")
    table.add_column("Cache read", justify="right")
    table.add_column("Cache write", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Tools / notes", overflow="fold")
    for e in detail.timeline:
        notes = ", ".join(e.tool_names)
        if e.is_sidechain:
            notes = f"[dim]subagent[/dim] {notes}".strip()
        if e.miss_cause:
            style = "dim" if e.miss_cause == "context_reset" else "red"
            notes = f"[{style}]cache miss: {_CAUSE_LABELS[e.miss_cause]}[/{style}] {notes}".strip()
        table.add_row(
            _local(e.timestamp, "%H:%M:%S"),
            e.model,
            tokens(e.usage.prompt_tokens),
            tokens(e.usage.cache_read_tokens),
            tokens(e.usage.cache_write_tokens),
            tokens(e.usage.output_tokens),
            money(e.cost.total) if e.cost else "?",
            notes,
        )
    console.print(table)
    if detail.loops:
        render_loops(console, LoopReport(findings=detail.loops, projects={}))


_SEVERITY = {
    "high": "[bold red]HIGH[/bold red]",
    "medium": "[yellow]MEDIUM[/yellow]",
    "low": "[cyan]LOW[/cyan]",
    "info": "[dim]INFO[/dim]",
}
_STATUS = {
    "pass": "[green]✔ pass[/green]",
    "warn": "[yellow]▲ warn[/yellow]",
    "fail": "[red]✖ fail[/red]",
    "n/a": "[dim]- n/a[/dim]",
}
_SAVINGS_KIND = {"reprice": "repriced", "estimate": "estimate", "upper_bound": "up to"}


def render_insights(console: Console, report: InsightsReport) -> None:
    p = report.profile
    if not p.requests:
        console.print("No requests in the selected range. Run `claude-lens import` first?")
        return
    mix = ", ".join(f"{m.key} {m.share:.0%}" for m in p.models[:4])
    efforts = ", ".join(f"{m.key} {m.share:.0%}" for m in p.efforts)
    console.print(
        f"[bold]Usage profile[/bold]  {p.sessions} sessions, {p.requests:,} requests, "
        f"{money(p.cost)} API-equivalent "
        f"(Claude Code {money(p.claude_code_cost)}, API apps {money(p.api_cost)})\n"
        f"  Models by cost: {mix}\n"
        f"  Effort by cost: {efforts}\n"
        f"  Thinking share of output: {_pct(p.thinking_share)}, subagents' share of cost: "
        f"{_pct(p.subagent_cost_share)}, cache hit ratio: {_pct(p.cache_hit_ratio)}\n"
    )
    if p.outcomes:
        o = p.outcomes.total
        per_commit = "-" if o.cost_per_commit is None else money(o.cost_per_commit)
        console.print(
            f"  Git outcome ({o.sessions} sessions in repositories, "
            f"{p.outcomes.untracked_sessions} outside): {o.commits} commits, "
            f"{per_commit} per commit, {o.edits_no_commit} edited without a commit\n"
        )
    if report.compare:
        render_comparison(console, report.compare)
    if not report.insights:
        console.print("No recommendations: nothing in this range crossed a threshold.")
    for n, i in enumerate(report.insights, 1):
        saving = ""
        if i.savings_usd:
            kind = _SAVINGS_KIND[i.savings_kind or "estimate"]
            saving = f"  [bold]{kind} {money(i.savings_usd)}[/bold]"
        console.print(f"{n}. {_SEVERITY[i.severity]} [bold]{i.title}[/bold]{saving}")
        console.print(f"   {i.finding}", highlight=False)
        console.print(f"   [green]→[/green] {i.recommendation}", highlight=False)
        if i.evidence:
            shown = "; ".join(i.evidence[:3]) + (" …" if len(i.evidence) > 3 else "")
            console.print(f"   [dim]{shown}[/dim]", highlight=False)
        console.print(f"   [dim]confidence {i.confidence} · {i.source}[/dim]\n", highlight=False)
    table = Table(title="Best practices", title_justify="left")
    table.add_column("Status")
    table.add_column("Practice")
    table.add_column("Evidence", overflow="fold")
    for pr in report.practices:
        table.add_row(_STATUS[pr.status], pr.title, pr.evidence)
    console.print(table)
    console.print(
        "[dim]Savings are API-equivalent list prices; overlapping recommendations can count the "
        "same tokens twice. Logs show cost, not quality: treat cheaper models and lower effort "
        "as experiments.[/dim]"
    )


def render_coach(console: Console, result: CoachResult) -> None:
    console.rule("Coach")
    console.print(Markdown(result.text))
    cost = ""
    if result.cost is not None:
        note = (
            " API-equivalent, not billed on a subscription"
            if result.backend == "claude-code"
            else ""
        )
        cost = f", {money(result.cost)}{note}"
    console.print(
        f"[dim]{result.model} via {result.backend}, {tokens(result.usage.prompt_tokens)} in / "
        f"{tokens(result.usage.output_tokens)} out{cost}[/dim]"
    )


def render_whatif(console: Console, result: WhatIf, selection: str) -> None:
    if not result.requests:
        console.print(f"No priced requests matched ({selection}).")
        return
    console.print(
        f"{result.requests:,} requests ({selection}): {money(result.current_cost)} as recorded, "
        f"{money(result.target_cost)} on {result.target}: "
        f"[bold]{money(result.savings)}[/bold] difference."
    )
    console.print(
        "[dim]Same token counts repriced. Another model produces different token counts and "
        "different results: this is the price of the experiment, not its outcome.[/dim]"
    )


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


# -- trends ------------------------------------------------------------------------------

_ARROW_STYLE = {"better": "green", "worse": "red", "same": "dim", "changed": "dim", "n/a": "dim"}


def metric_value(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    return money(value) if unit == "usd" else f"{value:.0%}"


def _delta(change: MetricChange) -> str:
    if change.delta is None or change.verdict == "same":
        return ""
    if change.unit == "usd":
        sign = "+" if change.delta >= 0 else ""
        return f"{sign}{money(change.delta)}"
    return f"{change.delta * 100:+.0f} pp"


def change_marker(change: MetricChange) -> str:
    """An arrow for the direction of the change, colored by whether it is an improvement."""
    if change.verdict == "n/a":
        return ""
    if change.verdict == "same":
        return "[dim]=[/dim]"
    arrow = "↑" if (change.delta or 0) > 0 else "↓"
    return f"[{_ARROW_STYLE[change.verdict]}]{arrow}[/{_ARROW_STYLE[change.verdict]}]"


def render_trends(console: Console, report: TrendsReport) -> None:
    if not any(w.requests for w in report.weeks):
        console.print("No requests in these weeks. Run `claude-lens import` first?")
        return
    title = "Weekly trends" + (f" ({report.project})" if report.project else "")
    table = Table(title=title, title_justify="left")
    table.add_column("Metric")
    for w in report.weeks:
        table.add_column(w.week[2:] + ("*" if w.partial else ""), justify="right")
    table.add_column("Change", justify="right")
    previous: list[WeekMetrics | None] = [None, *report.weeks[:-1]]
    for m in report.metrics:
        cells = []
        for week, before in zip(report.weeks, previous, strict=True):
            change = next(c for c in changes(week, before) if c.id == m.id)
            marker = change_marker(change)
            cells.append(metric_value(change.current, m.unit) + (f" {marker}" if marker else ""))
        last = next(c for c in report.changes if c.id == m.id)
        table.add_row(m.label, *cells, f"{_delta(last)} {change_marker(last)}".strip())
    table.add_row("[dim]Sessions[/dim]", *[f"[dim]{w.sessions}[/dim]" for w in report.weeks], "")
    console.print(table)
    for name, attr in (("model", "models"), ("effort", "efforts")):
        keys = sorted({m.key for w in report.weeks for m in getattr(w, attr)})
        if not keys:
            continue
        mix = Table(title=f"Cost share by {name}", title_justify="left")
        mix.add_column(name.capitalize())
        for w in report.weeks:
            mix.add_column(w.week[2:] + ("*" if w.partial else ""), justify="right")
        for key in keys:
            shares = [{m.key: m.share for m in getattr(w, attr)}.get(key) for w in report.weeks]
            mix.add_row(key, *[_pct(s) for s in shares])
        console.print(mix)
    console.print(
        "[dim]* the current week, still running. Arrows compare with the week before: green is "
        "an improvement, red a regression, = about the same; thinking share has no good "
        "direction. Definitions match `claude-lens insights` (docs/trends.md).[/dim]"
    )


def render_comparison(console: Console, comparison: Comparison) -> None:
    before, now = comparison.previous, comparison.current
    console.print(
        f"[bold]Since last period[/bold]  {_local(now.since, '%Y-%m-%d')} … "
        f"{_local(now.until - timedelta(seconds=1), '%Y-%m-%d')} vs "
        f"{_local(before.since, '%Y-%m-%d')} … "
        f"{_local(before.until - timedelta(seconds=1), '%Y-%m-%d')}"
    )
    for c in comparison.changes:
        verdict = {"better": " better", "worse": " worse", "same": " same"}.get(c.verdict, "")
        parts = [
            f"  {c.label}: {metric_value(c.previous, c.unit)} → {metric_value(c.current, c.unit)}",
            _delta(c),
            f"{change_marker(c)}{verdict}",
        ]
        console.print(" ".join(p for p in parts if p).rstrip(), highlight=False)
    console.print()
