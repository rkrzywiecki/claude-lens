"""Terminal renderers: the branches the command tests do not reach (empty and edge cases)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console

from claude_lens import render
from claude_lens.analysis.cache import CacheDoctorReport, diagnose
from claude_lens.analysis.loops import LoopFinding
from claude_lens.analysis.session import session_detail
from claude_lens.insights.coach import CoachResult
from claude_lens.insights.engine import build_insights
from claude_lens.insights.models import (
    MetricChange,
    OutcomeSummary,
    TrendsReport,
)
from claude_lens.insights.trends import compare, trends
from claude_lens.insights.whatif import WhatIf
from claude_lens.models import (
    ImportStats,
    OutcomeReport,
    OutcomeRow,
    Report,
    ReportRow,
    RequestRecord,
    SessionOutcome,
    ToolCall,
    Usage,
)
from claude_lens.pricing import Pricing, load_pricing
from claude_lens.queries import Filters, LoopReport
from claude_lens.store import Store

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def console() -> Console:
    return Console(record=True, width=200, color_system=None)


@pytest.fixture
def pricing() -> Pricing:
    return load_pricing()


def text(console: Console) -> str:
    return console.export_text()


def rec(n: int, *, sidechain: bool = False, at: float | None = None, **usage: int) -> RequestRecord:
    return RequestRecord(
        message_id=f"m{n}",
        session_id="sess-1",
        project="/work/app",
        model="claude-opus-5-5",
        timestamp=T0 + timedelta(seconds=n * 30 if at is None else at),
        usage=Usage(input_tokens=10, output_tokens=100, **usage),
        is_sidechain=sidechain,
        tool_names=["Edit"],
    )


def test_import_reports_malformed_lines(console: Console) -> None:
    render.render_import(console, ImportStats(files_seen=1, bad_lines=2), 0)
    assert "Skipped 2 malformed lines" in text(console)


def test_report_marks_unpriced_rows_and_labels_sessions(console: Console) -> None:
    row = ReportRow(
        key="abcdef1234", label="2026-10-12 12:00  ~/work", requests=1, unpriced_requests=1
    )
    total = ReportRow(key="total", requests=1, unpriced_requests=1)
    report = Report(group_by="session", rows=[row], total=total, unpriced_models=["claude-x"])
    render.render_report(console, report)
    out = text(console)
    assert "abcdef12  2026-10-12 12:00  ~/work" in out
    assert "$0.00*" in out
    assert "No price for claude-x" in out


def test_report_without_rows(console: Console) -> None:
    render.render_report(console, Report(group_by="day", rows=[], total=ReportRow(key="total")))
    assert "No requests" in text(console)


def test_outcome_report_without_rows(console: Console) -> None:
    empty = OutcomeReport(
        group_by="model", rows=[], total=OutcomeRow(key="total"), untracked_sessions=3
    )
    render.render_outcome_report(console, empty)
    assert text(console).strip()


def test_outcome_line() -> None:
    assert render.outcome_line(None)
    outcome = SessionOutcome(
        session_id="s",
        repo="/r",
        commits=2,
        files_changed=3,
        lines_added=10,
        lines_removed=4,
        window_end=T0,
        refs_hash="h",
    )
    assert "2" in render.outcome_line(outcome)


def test_cache_doctor_empty_and_clean(console: Console, pricing: Pricing) -> None:
    render.render_cache_doctor(console, CacheDoctorReport(sessions=[], advice=[]))
    assert "No requests" in text(console)
    clean = diagnose([rec(0, cache_read_tokens=10_000), rec(1, cache_read_tokens=11_000)], pricing)
    render.render_cache_doctor(console, clean)
    assert "No avoidable cache misses found." in text(console)


def test_loops_table(console: Console) -> None:
    finding = LoopFinding(
        kind="file_churn",
        session_id="sess-1",
        tool_names=["Edit"],
        count=6,
        first=T0,
        last=T0 + timedelta(minutes=5),
        target="/work/app/a.py",
    )
    render.render_loops(console, LoopReport(findings=[finding], projects={"sess-1": "/work/app"}))
    out = text(console)
    assert "file edited repeatedly" in out
    assert "/work/app/a.py" in out


def test_session_marks_subagents_misses_and_loops(console: Console, pricing: Pricing) -> None:
    records = [
        rec(0, cache_write_5m_tokens=50_000),
        rec(1, at=600, cache_write_5m_tokens=51_000),
        rec(2, sidechain=True),
    ]
    calls = [
        ToolCall(
            tool_use_id=f"t{i}",
            message_id="m0",
            session_id="sess-1",
            timestamp=T0 + timedelta(seconds=i),
            tool_name="Edit",
            input_hash=str(i),
            target="/a.py",
        )
        for i in range(5)
    ]
    render.render_session(console, session_detail(records, calls, pricing))
    out = text(console)
    assert "subagent" in out
    assert "cache miss: TTL expired" in out
    assert "file edited repeatedly" in out


def _store_with(records: list[RequestRecord]) -> Store:
    store = Store(Path(":memory:"))
    store.upsert(records)
    return store


def test_insights_empty_and_without_findings(console: Console, pricing: Pricing) -> None:
    with Store(Path(":memory:")) as store:
        render.render_insights(
            console, build_insights(store, pricing, Filters(), check_files=False)
        )
    assert "No requests" in text(console)

    with _store_with([rec(0, cache_read_tokens=9_000)]) as store:
        report = build_insights(store, pricing, Filters(), check_files=False)
    summary = OutcomeSummary(
        total=OutcomeRow(key="total", sessions=1, sessions_with_commits=1, commits=2, cost=1.0),
        by_model=[],
        by_effort=[],
        untracked_sessions=0,
    )
    report = report.model_copy(
        update={"insights": [], "profile": report.profile.model_copy(update={"outcomes": summary})}
    )
    render.render_insights(console, report)
    out = text(console)
    assert "No recommendations" in out
    assert "Best practices" in out


def test_insights_with_compare_block(console: Console, pricing: Pricing) -> None:
    with _store_with([rec(0), rec(1, at=8 * 86_400)]) as store:
        filters = Filters(since=T0 + timedelta(days=7), until=T0 + timedelta(days=14))
        report = build_insights(store, pricing, filters, check_files=False)
        report = report.model_copy(update={"compare": compare(store, pricing, filters)})
    render.render_insights(console, report)
    assert "Since last period" in text(console)


@pytest.mark.parametrize(
    ("backend", "cost", "expected"),
    [
        ("claude-code", 0.08, "not billed on a subscription"),
        ("api", 0.08, "via api"),
        ("api", None, "via api"),
    ],
)
def test_coach_footer(console: Console, backend: str, cost: float | None, expected: str) -> None:
    result = CoachResult(
        text="## Plan\n- clear more often",
        model="claude-opus-5-5",
        backend=backend,  # type: ignore[arg-type]
        usage=Usage(input_tokens=4_800, output_tokens=2_100),
        cost=cost,
    )
    render.render_coach(console, result)
    out = text(console)
    assert "Plan" in out
    assert expected in out


def test_whatif_without_requests(console: Console) -> None:
    empty = WhatIf(
        target="claude-sonnet-5-5",
        requests=0,
        current_cost=0,
        target_cost=0,
        unpriced_requests=0,
    )
    render.render_whatif(console, empty, "model ~ opus")
    assert "No priced requests matched" in text(console)


def test_trends_empty_and_with_weeks(console: Console, pricing: Pricing) -> None:
    render.render_trends(console, TrendsReport(project=None, metrics=[], weeks=[], changes=[]))
    assert "No requests in these weeks" in text(console)

    with _store_with([rec(0), rec(1, at=8 * 86_400)]) as store:
        report = trends(store, pricing, weeks=2, until=(T0 + timedelta(days=9)).date())
    render.render_trends(console, report)
    out = text(console)
    assert "Weekly trends" in out
    assert "Cost share by model" in out


def test_delta_and_markers() -> None:
    usd = MetricChange(
        id="cost",
        label="Cost",
        unit="usd",
        better="lower",
        current=3.0,
        previous=5.0,
        delta=-2.0,
        verdict="better",
    )
    assert render._delta(usd) == "-$2.00"
    up = usd.model_copy(update={"delta": 1.5, "verdict": "worse"})
    assert render._delta(up) == "+$1.50"
    share = usd.model_copy(update={"unit": "share", "delta": 0.1})
    assert render._delta(share) == "+10 pp"
    assert render.change_marker(usd.model_copy(update={"verdict": "n/a"})) == ""
    assert "=" in render.change_marker(usd.model_copy(update={"verdict": "same"}))
    assert render.metric_value(None, "usd") == "-"
    assert render.metric_value(0.25, "share") == "25%"


def test_insights_list_savings_and_evidence(console: Console, pricing: Pricing) -> None:
    from claude_lens.insights.models import Insight

    with _store_with([rec(0)]) as store:
        report = build_insights(store, pricing, Filters(), check_files=False)
    found = Insight(
        id="model.x",
        category="model",
        scope="claude_code",
        severity="high",
        confidence="medium",
        title="Try Sonnet",
        finding="Opus for light work.",
        recommendation="Use `/model sonnet`.",
        evidence=["a", "b", "c", "d"],
        savings_usd=12.5,
        savings_kind="upper_bound",
        source="https://code.claude.com/docs/en/costs",
    )
    render.render_insights(console, report.model_copy(update={"insights": [found]}))
    out = text(console)
    assert "1. HIGH Try Sonnet  up to $12.50" in out
    assert "a; b; c …" in out
    assert "confidence medium" in out
