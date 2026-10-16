from datetime import UTC, datetime, timedelta

import pytest

from claude_lens.analysis.cache import TTL_1H, diagnose
from claude_lens.analysis.loops import detect_loops
from claude_lens.analysis.session import session_detail
from claude_lens.fingerprint import (
    MESSAGES_START,
    SYSTEM,
    TOOLS,
    describe_segment,
    first_difference,
    segment_fingerprints,
    tool_input_hash,
    tool_target,
)
from claude_lens.models import RequestRecord, ToolCall, Usage
from claude_lens.pricing import Pricing, load_pricing

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def pricing() -> Pricing:
    return load_pricing()


def req(
    n: int,
    *,
    at: float,
    read: int = 0,
    write: int = 0,
    write_1h: int = 0,
    input_tokens: int = 10,
    model: str = "claude-opus-5-5",
    session: str = "s1",
    sidechain: bool = False,
    segments: list[str] | None = None,
) -> RequestRecord:
    """Request n, `at` seconds after T0."""
    return RequestRecord(
        message_id=f"m{n}",
        session_id=session,
        project="/work/app",
        model=model,
        timestamp=T0 + timedelta(seconds=at),
        usage=Usage(
            input_tokens=input_tokens,
            cache_read_tokens=read,
            cache_write_5m_tokens=write,
            cache_write_1h_tokens=write_1h,
            output_tokens=100,
        ),
        is_sidechain=sidechain,
        segments=segments,
    )


def call(
    n: int,
    name: str = "Bash",
    *,
    input_hash: str = "h",
    target: str | None = None,
    error: bool | None = False,
    session: str = "s1",
) -> ToolCall:
    return ToolCall(
        tool_use_id=f"t{n}",
        message_id=f"m{n}",
        session_id=session,
        timestamp=T0 + timedelta(seconds=n),
        tool_name=name,
        input_hash=input_hash,
        target=target,
        is_error=error,
    )


# -- fingerprints -------------------------------------------------------------


def test_segments_ignore_cache_control_and_string_form() -> None:
    a = {
        "system": "You are helpful.",
        "tools": [{"name": "Bash"}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    b = {
        "system": [{"type": "text", "text": "You are helpful.", "cache_control": {"x": 1}}],
        "tools": [{"name": "Bash", "cache_control": {"x": 1}}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "max_tokens": 5,  # not part of the cached prefix
    }
    assert segment_fingerprints(a) == segment_fingerprints(b)


def test_first_difference_points_at_changed_segment() -> None:
    base = {"system": "s", "tools": [], "messages": [{"role": "user", "content": "a"}]}
    grown = {**base, "messages": [*base["messages"], {"role": "assistant", "content": "b"}]}
    other_system = {**grown, "system": "s2"}
    with_thinking = {**grown, "thinking": {"type": "adaptive"}}
    fp = segment_fingerprints
    assert first_difference(fp(base) or [], fp(grown) or []) is None  # pure growth
    assert first_difference(fp(grown) or [], fp(other_system) or []) == SYSTEM
    assert first_difference(fp(grown) or [], fp(with_thinking) or []) == 2
    assert describe_segment(TOOLS) == "tool definitions changed"
    assert describe_segment(MESSAGES_START) == "message 1 of the history changed"
    assert segment_fingerprints({"messages": "nope"}) is None


def test_tool_input_hash_and_target() -> None:
    assert tool_input_hash({"a": 1, "b": 2}) == tool_input_hash({"b": 2, "a": 1})
    assert tool_input_hash({"a": 1}) != tool_input_hash({"a": 2})
    assert tool_target({"file_path": "/x.py", "old_string": "a"}) == "/x.py"
    assert tool_target({"command": "ls"}) is None
    assert tool_target("not a dict") is None


# -- cache doctor -------------------------------------------------------------


def test_steady_growth_is_not_a_miss(pricing: Pricing) -> None:
    records = [req(i, at=i * 30, read=10_000 + i * 1_000, write=1_000) for i in range(5)]
    [session] = diagnose(records, pricing).sessions
    assert session.misses == []
    assert session.penalty == 0


def test_short_pause_with_5m_ttl_is_avoidable(pricing: Pricing) -> None:
    records = [req(0, at=0, write=50_000), req(1, at=600, write=51_000)]
    report = diagnose(records, pricing)
    [miss] = report.sessions[0].misses
    assert miss.cause == "ttl_expired"
    assert miss.avoidable
    assert miss.lost_tokens == 50_000
    # 50k re-written at 1.25 x $4 instead of read at $0.20 per million.
    assert miss.penalty == pytest.approx(50_000 * (4.0 * 1.25 - 0.20) / 1e6)
    assert any("1-hour cache TTL" in line for line in report.advice)


def test_long_pause_is_unavoidable(pricing: Pricing) -> None:
    records = [req(0, at=0, write=50_000), req(1, at=7_200, write=51_000)]
    [miss] = diagnose(records, pricing).sessions[0].misses
    assert miss.cause == "ttl_expired"
    assert not miss.avoidable


def test_1h_ttl_is_tracked(pricing: Pricing) -> None:
    records = [req(0, at=0, write_1h=50_000), req(1, at=1_800, write_1h=51_000)]
    [miss] = diagnose(records, pricing).sessions[0].misses
    assert miss.cause == "unexplained"  # 30 minutes is within the 1-hour TTL
    assert miss.ttl_seconds == TTL_1H


def test_proxy_segments_name_the_changed_part(pricing: Pricing) -> None:
    before = ["tools", "sys-a", "params", "m1"]
    after = ["tools", "sys-b", "params", "m1", "m2"]
    records = [
        req(0, at=0, write=50_000, segments=before),
        req(1, at=10, write=52_000, segments=after),
    ]
    report = diagnose(records, pricing)
    [miss] = report.sessions[0].misses
    assert (miss.cause, miss.detail) == ("prefix_changed", "system prompt changed")
    assert any("system prompt changed" in line for line in report.advice)


def test_shrinking_prompt_is_a_reset_without_penalty(pricing: Pricing) -> None:
    records = [req(0, at=0, read=150_000, write=1_000), req(1, at=10, write=20_000)]
    [miss] = diagnose(records, pricing).sessions[0].misses
    assert miss.cause == "context_reset"
    assert miss.penalty == 0
    assert not miss.avoidable


def test_streams_are_per_model_and_skip_subagents(pricing: Pricing) -> None:
    records = [
        req(0, at=0, write=50_000),
        req(1, at=5, write=4_000, model="claude-haiku-5-5"),  # side call on another model
        req(2, at=8, write=3_000, sidechain=True),  # subagent with its own prefix
        req(3, at=10, read=50_000, write=1_000),
    ]
    assert diagnose(records, pricing).sessions[0].misses == []


def test_requests_without_any_caching_are_counted(pricing: Pricing) -> None:
    records = [req(0, at=0, input_tokens=60_000), req(1, at=5, input_tokens=61_000)]
    report = diagnose(records, pricing)
    assert report.sessions[0].uncached_requests == 2
    assert any("no prompt cache" in line for line in report.advice)


def test_sessions_sorted_by_avoidable_penalty(pricing: Pricing) -> None:
    calm = [req(i, at=i, read=1_000 * i, session="calm") for i in range(3)]
    busted = [
        req(10, at=0, write=80_000, session="busted"),
        req(11, at=60, write=81_000, session="busted"),
    ]
    report = diagnose(calm + busted, pricing, top=1)
    assert [s.session_id for s in report.sessions] == ["busted"]
    assert report.avoidable_penalty > 0


# -- loops --------------------------------------------------------------------


def test_repeated_identical_calls() -> None:
    calls = [call(i, input_hash="same") for i in range(3)] + [call(9, input_hash="other")]
    [finding] = detect_loops(calls)
    assert (finding.kind, finding.count) == ("repeated_call", 3)


def test_routine_tools_are_not_repeated_calls() -> None:
    calls = [call(i, "mcp__ccd_turn__update_plan", input_hash="same") for i in range(5)]
    assert detect_loops(calls) == []


def test_error_streak_ignores_unknown_results() -> None:
    calls = [
        call(0, error=True, input_hash="a"),
        call(1, error=None, input_hash="b"),
        call(2, error=True, input_hash="c"),
        call(3, error=True, input_hash="d"),
        call(4, error=False, input_hash="e"),
        call(5, error=True, input_hash="f"),
    ]
    [finding] = detect_loops(calls)
    assert (finding.kind, finding.count) == ("error_streak", 3)


def test_file_churn() -> None:
    calls = [call(i, "Edit", input_hash=str(i), target="/a.py") for i in range(5)]
    calls += [call(9, "Read", input_hash="r", target="/a.py")]
    [finding] = detect_loops(calls)
    assert (finding.kind, finding.count, finding.target) == ("file_churn", 5, "/a.py")


def test_loops_are_per_session() -> None:
    calls = [call(i, input_hash="x", session=f"s{i}") for i in range(3)]
    assert detect_loops(calls) == []


# -- session detail -----------------------------------------------------------


def test_session_detail(pricing: Pricing) -> None:
    records = [
        req(0, at=0, write=100_000),
        req(1, at=600, write=101_000),  # 5-minute entry expired
        req(2, at=610, read=101_000, write=2_000),
    ]
    calls = [call(i, "Edit", input_hash=str(i), target="/a.py") for i in range(5)]
    detail = session_detail(records, calls, pricing)
    assert [e.miss_cause for e in detail.timeline] == [None, "ttl_expired", None]
    assert detail.timeline[2].context_share == pytest.approx(103_010 / 1_000_000)
    assert detail.context.peak_prompt_tokens == 103_010
    assert detail.context.average_growth == pytest.approx((103_010 - 100_010) / 2)
    assert detail.cost == pytest.approx(sum(e.cost.total for e in detail.timeline if e.cost))
    assert [f.kind for f in detail.loops] == ["file_churn"]
