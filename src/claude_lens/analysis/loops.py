"""Loop detection over tool calls: work an agent repeats without making progress.

Three patterns, per session:

- `repeated_call`: the same tool with the same input (by hash) called again and again.
- `error_streak`: consecutive tool calls whose results were errors.
- `file_churn`: the same file edited or rewritten many times.

These are signals, not verdicts: re-running a test command after each fix is a repeated call
too. Thresholds are deliberately conservative.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from fnmatch import fnmatchcase
from typing import Literal

from pydantic import BaseModel

from claude_lens.models import ToolCall

LoopKind = Literal["repeated_call", "error_streak", "file_churn"]

REPEATED_CALL_MIN = 3
ERROR_STREAK_MIN = 3
FILE_CHURN_MIN = 5

# Tools whose identical inputs are routine (progress and task-list updates), not a loop.
REPEAT_IGNORED = ("*update_plan", "TodoWrite", "TaskUpdate", "TaskList")

# Tools that change a file named by their input (Claude Code and the text editor tool).
EDIT_TOOLS = frozenset(
    {"Edit", "MultiEdit", "Write", "NotebookEdit", "str_replace_based_edit_tool"}
)


class LoopFinding(BaseModel):
    kind: LoopKind
    session_id: str
    tool_names: list[str]
    count: int
    first: datetime
    last: datetime
    target: str | None = None


def detect_loops(calls: Iterable[ToolCall]) -> list[LoopFinding]:
    """Findings ordered by session, then by count (highest first)."""
    by_session: dict[str, list[ToolCall]] = defaultdict(list)
    for call in calls:
        by_session[call.session_id].append(call)
    findings: list[LoopFinding] = []
    for session_calls in by_session.values():
        session_calls.sort(key=lambda c: c.timestamp)
        found = (
            _repeated_calls(session_calls)
            + _error_streaks(session_calls)
            + _file_churn(session_calls)
        )
        findings.extend(sorted(found, key=lambda f: (-f.count, f.first)))
    return findings


def _repeated_calls(calls: list[ToolCall]) -> list[LoopFinding]:
    groups: dict[tuple[str, str], list[ToolCall]] = defaultdict(list)
    for call in calls:
        if not any(fnmatchcase(call.tool_name, pattern) for pattern in REPEAT_IGNORED):
            groups[(call.tool_name, call.input_hash)].append(call)
    return [
        _finding("repeated_call", group, target=group[0].target)
        for group in groups.values()
        if len(group) >= REPEATED_CALL_MIN
    ]


def _error_streaks(calls: list[ToolCall]) -> list[LoopFinding]:
    findings: list[LoopFinding] = []
    streak: list[ToolCall] = []
    for call in [*calls, None]:
        if call is not None and call.is_error:
            streak.append(call)
            continue
        if call is not None and call.is_error is None:
            continue  # result unknown: neither extends nor breaks the streak
        if len(streak) >= ERROR_STREAK_MIN:
            findings.append(_finding("error_streak", streak))
        streak = []
    return findings


def _file_churn(calls: list[ToolCall]) -> list[LoopFinding]:
    groups: dict[str, list[ToolCall]] = defaultdict(list)
    for call in calls:
        if call.tool_name in EDIT_TOOLS and call.target:
            groups[call.target].append(call)
    return [
        _finding("file_churn", group, target=target)
        for target, group in groups.items()
        if len(group) >= FILE_CHURN_MIN
    ]


def _finding(kind: LoopKind, calls: list[ToolCall], target: str | None = None) -> LoopFinding:
    names: list[str] = []
    for call in calls:
        if call.tool_name not in names:
            names.append(call.tool_name)
    return LoopFinding(
        kind=kind,
        session_id=calls[0].session_id,
        tool_names=names,
        count=len(calls),
        first=calls[0].timestamp,
        last=calls[-1].timestamp,
        target=target,
    )
