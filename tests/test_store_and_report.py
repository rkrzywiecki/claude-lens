import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_lens.models import RequestRecord, Usage
from claude_lens.pricing import load_pricing
from claude_lens.report import build_report, shorten_home
from claude_lens.store import Store, StoreError

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def record(
    message_id: str,
    *,
    at: datetime = T0,
    model: str = "claude-opus-5-5",
    project: str = "/work/app",
    session: str = "sess-1",
    **usage: int,
) -> RequestRecord:
    return RequestRecord(
        message_id=message_id,
        session_id=session,
        project=project,
        model=model,
        timestamp=at,
        usage=Usage(**usage),
        tool_names=["Bash"],
    )


def test_roundtrip(store: Store) -> None:
    original = record("m1", input_tokens=5, output_tokens=7, cache_write_1h_tokens=3)
    store.upsert([original])
    assert list(store.requests()) == [original]


def test_upsert_replaces_by_message_id(store: Store) -> None:
    store.upsert([record("m1", output_tokens=1)])
    store.upsert([record("m1", output_tokens=2)])
    [stored] = store.requests()
    assert stored.usage.output_tokens == 2


def test_time_filters_compare_instants_not_strings(store: Store) -> None:
    store.upsert([record("early", at=T0), record("late", at=T0 + timedelta(hours=2))])
    # Same instant as T0 + 1h, expressed in UTC+2: string order would get this wrong.
    since = (T0 + timedelta(hours=1)).astimezone(datetime.now().astimezone().tzinfo)
    assert [r.message_id for r in store.requests(since=since)] == ["late"]
    assert [r.message_id for r in store.requests(until=since)] == ["early"]


def test_project_filter_is_substring(store: Store) -> None:
    store.upsert([record("a", project="/work/app"), record("b", project="/work/other")])
    assert [r.message_id for r in store.requests(project="app")] == ["a"]


def test_rejects_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "lens.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(StoreError):
        Store(path)


def test_report_groups_and_totals() -> None:
    records = [
        record("a", model="claude-opus-5-5", output_tokens=1_000_000),
        record("b", model="claude-sonnet-5-5", output_tokens=1_000_000),
        record("c", model="claude-sonnet-5-5", output_tokens=1_000_000),
    ]
    report = build_report(records, load_pricing(), "model")
    assert [r.key for r in report.rows] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert report.rows[0].cost.total == pytest.approx(20.0)
    assert report.rows[1].requests == 2
    assert report.total.cost.total == pytest.approx(40.0)


def test_report_days_are_chronological_and_top_keeps_latest() -> None:
    records = [record(f"m{i}", at=T0 + timedelta(days=i), output_tokens=10 - i) for i in range(3)]
    report = build_report(records, load_pricing(), "day", top=2)
    days = [r.key for r in report.rows]
    assert days == sorted(days)
    assert len(days) == 2
    assert report.total.requests == 3


def test_report_flags_unpriced_models() -> None:
    records = [record("a", model="claude-mystery-1", output_tokens=5), record("b", output_tokens=5)]
    report = build_report(records, load_pricing(), "project")
    assert report.unpriced_models == ["claude-mystery-1"]
    assert report.total.unpriced_requests == 1
    assert report.total.requests == 2


def test_cache_hit_ratio() -> None:
    records = [record("a", input_tokens=10, cache_write_5m_tokens=10, cache_read_tokens=80)]
    [row] = build_report(records, load_pricing(), "session").rows
    assert row.cache_hit_ratio == pytest.approx(0.8)
    assert row.label is not None and "/work/app" in row.label


def test_cache_hit_ratio_without_prompt_tokens_is_none() -> None:
    [row] = build_report([record("a")], load_pricing(), "model").rows
    assert row.cache_hit_ratio is None


def test_shorten_home() -> None:
    home = str(Path.home())
    assert shorten_home(home + "/Projects/x") == "~/Projects/x"
    assert shorten_home(home + "-other/x") == home + "-other/x"
    assert shorten_home("/opt/x") == "/opt/x"
