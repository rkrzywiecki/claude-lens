"""Trends: local ISO-week bucketing, metric definitions matching insights, compare, API."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_insights import call, rec
from typer.testing import CliRunner

from claude_lens.cli import app as cli_app
from claude_lens.config import Settings
from claude_lens.insights import rules
from claude_lens.insights.coach import coach_payload
from claude_lens.insights.engine import build_insights
from claude_lens.insights.models import MetricInfo
from claude_lens.insights.trends import (
    METRICS,
    compare,
    local_midnight,
    measure,
    previous_range,
    trends,
    verdict,
    week_label,
    week_start,
)
from claude_lens.models import RequestRecord, SessionMeta, ToolCall
from claude_lens.pricing import Pricing, load_pricing
from claude_lens.queries import Filters
from claude_lens.store import Store
from claude_lens.web.app import create_dashboard

runner = CliRunner()


@pytest.fixture
def pricing() -> Pricing:
    return load_pricing()


@pytest.fixture
def warsaw(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run in a fixed local time zone with DST (Europe/Warsaw, UTC+2 in summer, +1 in winter)."""
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def at(when: datetime, n: int, **kw: object) -> RequestRecord:
    """`rec` placed at an absolute time."""
    r = rec(n, **kw)  # type: ignore[arg-type]
    return r.model_copy(update={"timestamp": when})


# -- weeks ---------------------------------------------------------------------------


def test_week_helpers() -> None:
    assert week_start(date(2026, 10, 16)) == date(2026, 10, 12)  # Friday -> Monday
    assert week_start(date(2026, 10, 12)) == date(2026, 10, 12)
    assert week_label(date(2026, 10, 12)) == "2026-W42"
    assert week_label(date(2025, 12, 29)) == "2026-W01"  # ISO year differs from calendar year


@pytest.mark.usefixtures("warsaw")
def test_weeks_follow_local_time(store: Store, pricing: Pricing) -> None:
    # Sunday 23:30 local (21:30 UTC) belongs to W41; Monday 00:30 local (Sunday 22:30 UTC) to
    # W42, although both are on Sunday in UTC.
    sunday_late = datetime(2026, 10, 11, 21, 30, tzinfo=UTC)
    monday_early = datetime(2026, 10, 11, 22, 30, tzinfo=UTC)
    store.upsert([at(sunday_late, 1, session="a"), at(monday_early, 2, session="b")])
    report = trends(store, pricing, weeks=2, until=date(2026, 10, 12), today=date(2026, 10, 30))
    assert [w.week for w in report.weeks] == ["2026-W41", "2026-W42"]
    assert [w.requests for w in report.weeks] == [1, 1]
    assert not any(w.partial for w in report.weeks)
    assert report.weeks[1].since == local_midnight(date(2026, 10, 12))
    assert report.weeks[1].since.utcoffset() == timedelta(hours=2)


@pytest.mark.usefixtures("warsaw")
def test_week_across_dst_change_is_seven_local_days(store: Store, pricing: Pricing) -> None:
    # 2026-10-25: clocks go back an hour, so this week lasts 169 hours.
    report = trends(store, pricing, weeks=1, until=date(2026, 10, 25), today=date(2026, 10, 25))
    week = report.weeks[0]
    assert week.week == "2026-W43"
    assert week.partial
    assert week.until - week.since == timedelta(days=7, hours=1)
    assert week.until.astimezone().hour == 0


def test_trends_defaults_and_changes(store: Store, pricing: Pricing) -> None:
    today = date.today()
    monday = week_start(today)
    store.upsert(
        [
            at(local_midnight(monday) + timedelta(hours=10), 1, session="now"),
            at(local_midnight(monday - timedelta(weeks=1)) + timedelta(hours=10), 2, session="old"),
            at(local_midnight(monday - timedelta(weeks=1)) + timedelta(hours=11), 3, session="old"),
        ]
    )
    report = trends(store, pricing, weeks=3)
    assert len(report.weeks) == 3
    assert report.weeks[-1].partial and not report.weeks[0].partial
    assert [w.requests for w in report.weeks] == [0, 2, 1]
    cost = next(c for c in report.changes if c.id == "cost")
    assert cost.verdict == "better"  # one request instead of two
    assert cost.delta is not None and cost.delta < 0
    assert [m.id for m in report.metrics] == [m.id for m in METRICS]
    with pytest.raises(ValueError, match="at least 1"):
        trends(store, pricing, weeks=0)


# -- definitions match insights -----------------------------------------------------


def _week_of_activity() -> tuple[list[RequestRecord], list[ToolCall], dict[str, SessionMeta]]:
    records: list[RequestRecord] = []
    calls: list[ToolCall] = []
    # Long session that grows past the peak threshold without a reset.
    records += [rec(n, session="long", prompt=40_000 + n * 15_000, at=n * 60) for n in range(10)]
    # Long session with a reset: not a kitchen sink.
    records += [
        rec(n, session="reset", prompt=(160_000 if n == 4 else 30_000), at=n * 60) for n in range(6)
    ]
    # Multi-file change without plan mode, with file churn (rework).
    records += [rec(n, session="change", tools=["Edit"], at=n * 60) for n in range(10)]
    calls += [
        call(n, "Edit", session="change", target=f"/work/app/f{min(n, 4)}.py") for n in range(10)
    ]
    # Multi-file change in plan mode.
    records += [rec(n, session="planned", tools=["Edit"], at=n * 60) for n in range(5)]
    calls += [call(n, "Edit", session="planned", target=f"/work/app/g{n}.py") for n in range(5)]
    # An API application record: counts in cost, not in Claude Code shares.
    records.append(rec(1, session="api", source="proxy", prompt=10_000))
    metas = {
        "long": SessionMeta(session_id="long", source_file="x", human_prompts=10),
        "reset": SessionMeta(session_id="reset", source_file="x", human_prompts=9),
        "planned": SessionMeta(session_id="planned", source_file="x", permission_modes=["plan"]),
        "change": SessionMeta(session_id="change", source_file="x", interruptions=1),
    }
    return records, calls, metas


def test_metrics_match_insights_definitions(store: Store, pricing: Pricing) -> None:
    records, calls, metas = _week_of_activity()
    store.upsert(records)
    store.upsert_tool_calls(calls)
    store.upsert_session_meta(metas.values())
    since = local_midnight(date(2026, 10, 12))
    filters = Filters(since=since, until=since + timedelta(days=7))
    m = measure(store, pricing, filters)
    report = build_insights(store, pricing, filters, check_files=False)
    by_id = {i.id: i for i in report.insights}

    # Same totals and mixes as the insights profile.
    assert m.cost == pytest.approx(report.profile.cost)
    assert m.models == report.profile.models
    assert m.efforts == report.profile.efforts
    assert m.cache_hit_ratio == report.profile.cache_hit_ratio
    assert m.thinking_share == report.profile.thinking_share
    # Kitchen sink: the sessions the rule flags, out of the long sessions.
    assert m.long_sessions == 2
    assert m.kitchen_sink_sessions == len(by_id["context.kitchen_sink_sessions"].sessions) == 1
    assert m.kitchen_sink_share == 0.5
    # Plan mode: the sessions the rule flags are the unplanned ones.
    unplanned = len(by_id["workflow.no_plan_for_large_changes"].sessions)
    assert m.large_change_sessions == 2
    assert m.large_change_sessions - m.planned_large_change_sessions == unplanned == 1
    assert m.plan_mode_share == 0.5
    # Rework: the sessions the rule flags, out of all Claude Code sessions.
    assert m.rework_sessions == len(by_id["workflow.rework"].sessions) == 1
    assert m.claude_code_sessions == 4
    assert m.rework_share == 0.25
    # Context cost covers every Claude Code session; for the kitchen sink alone it equals the
    # rule's upper bound.
    ctx_long = sum(
        rules.context_carry_cost([r for r in records if r.session_id == s], pricing)
        for s in by_id["context.kitchen_sink_sessions"].sessions
    )
    assert by_id["context.kitchen_sink_sessions"].savings_usd == pytest.approx(ctx_long, abs=0.01)
    assert m.context_cost >= ctx_long > 0
    assert m.avoidable_cache_penalty >= 0


def test_verdicts() -> None:
    lower = MetricInfo(id="x", label="x", unit="usd", better="lower", description="")
    higher = MetricInfo(id="y", label="y", unit="share", better="higher", description="")
    neutral = MetricInfo(id="z", label="z", unit="share", better=None, description="")
    assert verdict(lower, -1.0, 5.0) == "better"
    assert verdict(lower, 1.0, 5.0) == "worse"
    assert verdict(lower, 0.05, 5.0) == "same"  # under 2% of the previous value
    assert verdict(higher, 0.2, 0.5) == "better"
    assert verdict(higher, -0.005, 0.5) == "same"
    assert verdict(neutral, 0.2, 0.3) == "changed"
    assert verdict(lower, None, None) == "n/a"


# -- compare ---------------------------------------------------------------------------


@pytest.mark.usefixtures("warsaw")
def test_previous_range_shifts_by_local_days() -> None:
    since = local_midnight(date(2026, 10, 26))
    until = local_midnight(date(2026, 11, 2))
    before = previous_range(since, until)
    # Across the DST change the previous week starts at local midnight too (169 hours long).
    assert before == (local_midnight(date(2026, 10, 19)), since)
    assert before[1] - before[0] == timedelta(days=7, hours=1)
    odd = datetime(2026, 10, 10, 12, tzinfo=UTC)
    assert previous_range(odd, odd + timedelta(hours=5)) == (
        odd - timedelta(hours=5),
        odd.astimezone(),
    )


def test_compare_block(store: Store, pricing: Pricing) -> None:
    # Previous week: a kitchen-sink session; this week: a long session that resets.
    week = local_midnight(date(2026, 10, 12))
    old = [
        at(
            week - timedelta(days=7) + timedelta(minutes=n),
            n,
            session="old",
            prompt=40_000 + n * 15_000,
        )
        for n in range(10)
    ]
    new = [
        at(week + timedelta(minutes=n), n, session="new", prompt=(160_000 if n == 4 else 30_000))
        for n in range(6)
    ]
    store.upsert(old + new)
    store.upsert_session_meta(
        [
            SessionMeta(session_id="old", source_file="a", human_prompts=10),
            SessionMeta(session_id="new", source_file="b", human_prompts=10),
        ]
    )
    result = compare(store, pricing, Filters(since=week, until=week + timedelta(days=7)))
    assert result.previous.since == week - timedelta(days=7)
    assert result.previous.until == week
    changes = {c.id: c for c in result.changes}
    assert changes["kitchen_sink_share"].previous == 1.0
    assert changes["kitchen_sink_share"].current == 0.0
    assert changes["kitchen_sink_share"].verdict == "better"
    assert changes["plan_mode_share"].verdict == "n/a"  # no multi-file sessions
    with pytest.raises(ValueError, match="start"):
        compare(store, pricing, Filters())

    report = build_insights(store, pricing, Filters(since=week), check_files=False)
    report.compare = result
    payload = coach_payload(report)
    block = payload["since_last_period"]
    assert block["previous"]["since"] == "2026-10-05"
    assert block["current"]["until"] == "2026-10-18"
    sink = next(m for m in block["metrics"] if m["metric"] == "Long sessions without reset")
    assert sink == {
        "metric": "Long sessions without reset",
        "unit": "share",
        "previous": 1.0,
        "current": 0.0,
        "better_when": "lower",
        "verdict": "better",
    }
    # Aggregates only: no session ids or paths.
    text = json.dumps(block)
    assert '"old"' not in text and '"new"' not in text and "/work" not in text
    assert "since_last_period" not in coach_payload(build_insights(store, pricing, Filters()))


# -- CLI and API -----------------------------------------------------------------------


def _example_db(tmp_path: Path) -> Path:
    db = tmp_path / "lens.db"
    week = local_midnight(week_start(date.today()))
    with Store(db) as store:
        store.upsert(
            [
                at(week + timedelta(hours=1), 1, session="a"),
                at(week - timedelta(weeks=1) + timedelta(hours=1), 2, session="b"),
                at(week + timedelta(hours=2), 3, session="c", project="/home/x/other"),
            ]
        )
    return db


def test_cli_trends(tmp_path: Path) -> None:
    db = _example_db(tmp_path)
    result = runner.invoke(cli_app, ["trends", "--weeks", "3", "--db", str(db), "--no-refresh"])
    assert result.exit_code == 0, result.output
    assert "Weekly trends" in result.output
    assert "Cache hit ratio" in result.output
    data = json.loads(
        runner.invoke(
            cli_app,
            [
                "trends",
                "--weeks",
                "2",
                "--project",
                "work",
                "--json",
                "--db",
                str(db),
                "--no-refresh",
            ],
        ).output
    )
    assert [w["requests"] for w in data["weeks"]] == [1, 1]
    assert data["project"] == "work"
    assert data["weeks"][-1]["partial"] is True


def test_cli_insights_compare(tmp_path: Path) -> None:
    db = _example_db(tmp_path)
    no_range = runner.invoke(cli_app, ["insights", "--compare", "--db", str(db), "--no-refresh"])
    assert no_range.exit_code == 2
    assert "--compare needs a range" in no_range.output
    result = runner.invoke(
        cli_app,
        [
            "insights",
            "--days",
            "7",
            "--compare",
            "--no-check-files",
            "--db",
            str(db),
            "--no-refresh",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Since last period" in result.output
    dry = runner.invoke(
        cli_app,
        [
            "insights",
            "--days",
            "7",
            "--compare",
            "--coach",
            "--dry-run",
            "--db",
            str(db),
            "--no-refresh",
        ],
    )
    assert "since_last_period" in json.loads(dry.output)


def test_api_trends(tmp_path: Path) -> None:
    db = _example_db(tmp_path)
    settings = Settings(db_path=db, transcripts_dir=tmp_path / "none")
    client = TestClient(
        create_dashboard(settings, open_store=lambda: Store(db)), base_url="http://127.0.0.1"
    )
    data = client.get("/api/trends", params={"weeks": 2}).json()
    assert [w["requests"] for w in data["weeks"]] == [1, 2]
    assert {"metrics", "changes", "project"} <= data.keys()
    assert data["weeks"][0]["kitchen_sink_share"] is None  # no long sessions: no value, not 0
    home = str(Path.home())
    only = client.get("/api/trends", params={"project": "/work/app"}).json()
    assert len(only["weeks"]) == 8
    assert sum(w["requests"] for w in only["weeks"]) == 2
    assert client.get("/api/trends", params={"weeks": 0}).status_code == 422
    assert client.get("/api/trends", params={"weeks": 105}).status_code == 422
    tilde = client.get("/api/trends", params={"project": "~/x"}).json()
    assert tilde["project"] == "~/x"
    assert home not in json.dumps(tilde)
