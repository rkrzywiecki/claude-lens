"""Per-session statistics the insight rules work on."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, Field

from claude_lens.analysis.cache import SessionCache, session_misses
from claude_lens.analysis.loops import EDIT_TOOLS, LoopFinding
from claude_lens.models import RequestRecord, SessionMeta, SessionOutcome, ToolCall
from claude_lens.pricing import Pricing, normalize_model

READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead"})
# The tool Claude Code delegates to subagents with ("Task" in older versions).
SUBAGENT_TOOLS = frozenset({"Agent", "Task"})
# Present when Claude ended a plan-mode turn with a plan.
PLAN_TOOLS = frozenset({"ExitPlanMode"})
# Model families from most to least capable; used to name cheaper alternatives.
BIG_MODEL_PREFIXES = ("claude-fable", "claude-mythos", "claude-opus")


def is_big_model(model: str) -> bool:
    return normalize_model(model).startswith(BIG_MODEL_PREFIXES)


class SessionStats(BaseModel):
    session_id: str
    project: str
    source: str
    started: datetime
    ended: datetime
    requests: list[RequestRecord] = Field(default_factory=list, repr=False)
    main_requests: int = 0
    subagent_requests: int = 0
    cost: float = 0.0
    main_cost: float = 0.0
    subagent_cost: float = 0.0
    big_model_main_cost: float = 0.0
    big_model_subagent_cost: float = 0.0
    output_tokens: int = 0
    thinking_tokens: int = 0
    main_tool_counts: Counter[str] = Field(default_factory=Counter)
    subagent_tool_counts: Counter[str] = Field(default_factory=Counter)
    edited_files: set[str] = Field(default_factory=set)
    efforts: Counter[str] = Field(default_factory=Counter)
    peak_prompt_tokens: int = 0
    peak_context_share: float | None = None
    cache: SessionCache | None = None
    loops: list[LoopFinding] = Field(default_factory=list)
    meta: SessionMeta | None = None
    # API-equivalent cost per (normalized) model.
    model_costs: dict[str, float] = Field(default_factory=dict)
    # Commits in the session's git repository; None when it ran outside one.
    outcome: SessionOutcome | None = None

    @property
    def edits(self) -> int:
        return sum(self.main_tool_counts[t] + self.subagent_tool_counts[t] for t in EDIT_TOOLS)

    @property
    def main_reads(self) -> int:
        return sum(self.main_tool_counts[t] for t in READ_TOOLS)

    @property
    def uses_subagents(self) -> bool:
        return self.subagent_requests > 0 or any(self.main_tool_counts[t] for t in SUBAGENT_TOOLS)

    @property
    def used_plan_mode(self) -> bool:
        modes = self.meta.permission_modes if self.meta else []
        return "plan" in modes or any(self.main_tool_counts[t] for t in PLAN_TOOLS)

    @property
    def thinking_share(self) -> float | None:
        return self.thinking_tokens / self.output_tokens if self.output_tokens else None

    @property
    def avg_main_output(self) -> float:
        main = [r for r in self.requests if not r.is_sidechain]
        return sum(r.usage.output_tokens for r in main) / len(main) if main else 0.0

    @property
    def is_light(self) -> bool:
        """Reading, answering and running commands, without changing files."""
        return self.edits == 0

    @property
    def human_prompts(self) -> int:
        return self.meta.human_prompts if self.meta else 0

    @property
    def resets(self) -> int:
        return self.cache.counts["context_reset"] if self.cache else 0

    @property
    def main_model(self) -> str | None:
        """The model that cost the most in this session."""
        return dominant(self.model_costs)

    @property
    def main_effort(self) -> str | None:
        """The effort most requests ran at."""
        return dominant(self.efforts)

    @property
    def label(self) -> str:
        return f"{self.session_id[:8]} {self.started.astimezone():%Y-%m-%d %H:%M}"


def dominant(values: Counter[str] | dict[str, float]) -> str | None:
    """The key with the largest value (ties: alphabetical)."""
    return min(values, key=lambda k: (-values[k], k)) if values else None


def build_session_stats(
    records: Iterable[RequestRecord],
    calls: Iterable[ToolCall],
    metas: dict[str, SessionMeta],
    loops: list[LoopFinding],
    pricing: Pricing,
    outcomes: dict[str, SessionOutcome] | None = None,
) -> list[SessionStats]:
    by_session: dict[str, list[RequestRecord]] = defaultdict(list)
    for record in records:
        by_session[record.session_id].append(record)
    calls_by_message: dict[str, list[ToolCall]] = defaultdict(list)
    for call in calls:
        calls_by_message[call.message_id].append(call)
    loops_by_session: dict[str, list[LoopFinding]] = defaultdict(list)
    for finding in loops:
        loops_by_session[finding.session_id].append(finding)

    sessions: list[SessionStats] = []
    for session_id, session_records in by_session.items():
        session_records.sort(key=lambda r: r.timestamp)
        first = session_records[0]
        stats = SessionStats(
            session_id=session_id,
            project=first.project,
            source=first.source,
            started=first.timestamp,
            ended=session_records[-1].timestamp,
            requests=session_records,
            cache=session_misses(session_records, pricing),
            loops=loops_by_session.get(session_id, []),
            meta=metas.get(session_id),
            outcome=(outcomes or {}).get(session_id),
        )
        for record in session_records:
            _add(stats, record, calls_by_message.get(record.message_id, []), pricing)
        sessions.append(stats)
    sessions.sort(key=lambda s: s.started)
    return sessions


def _add(
    stats: SessionStats, record: RequestRecord, calls: list[ToolCall], pricing: Pricing
) -> None:
    cost = pricing.cost(record.model, record.usage, record.speed)
    amount = cost.total if cost else 0.0
    stats.cost += amount
    model = normalize_model(record.model)
    stats.model_costs[model] = stats.model_costs.get(model, 0.0) + amount
    big = is_big_model(record.model)
    tools = stats.subagent_tool_counts if record.is_sidechain else stats.main_tool_counts
    if record.is_sidechain:
        stats.subagent_requests += 1
        stats.subagent_cost += amount
        stats.big_model_subagent_cost += amount if big else 0.0
    else:
        stats.main_requests += 1
        stats.main_cost += amount
        stats.big_model_main_cost += amount if big else 0.0
    stats.output_tokens += record.usage.output_tokens
    stats.thinking_tokens += record.thinking_tokens or 0
    # Tool calls count every call; records from before tool calls were stored have names only.
    counts = Counter(c.tool_name for c in calls) or Counter(record.tool_names)
    tools.update(counts)
    for call in calls:
        if call.tool_name in EDIT_TOOLS and call.target:
            stats.edited_files.add(call.target)
    if record.effort:
        stats.efforts[record.effort] += 1
    if record.usage.prompt_tokens > stats.peak_prompt_tokens:
        stats.peak_prompt_tokens = record.usage.prompt_tokens
        window = pricing.context_window(record.model)
        stats.peak_context_share = record.usage.prompt_tokens / window if window else None
