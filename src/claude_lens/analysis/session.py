"""One session in detail: request timeline, context growth, cache misses and loops."""

from __future__ import annotations

from datetime import datetime
from itertools import pairwise

from pydantic import BaseModel, computed_field

from claude_lens.analysis.cache import SessionCache, session_misses
from claude_lens.analysis.loops import LoopFinding, detect_loops
from claude_lens.models import CostBreakdown, RequestRecord, SessionOutcome, ToolCall, Usage
from claude_lens.pricing import Pricing


class TimelineEntry(BaseModel):
    message_id: str
    timestamp: datetime
    model: str
    usage: Usage
    cost: CostBreakdown | None
    tool_names: list[str]
    is_sidechain: bool
    stop_reason: str | None
    ttft_ms: float | None
    latency_ms: float | None
    # Share of the model's context window this request's prompt filled.
    context_share: float | None
    miss_cause: str | None = None


class ContextSummary(BaseModel):
    peak_prompt_tokens: int
    peak_share: float | None
    final_prompt_tokens: int
    # Average change of the main conversation's prompt from one request to the next.
    average_growth: float
    resets: int


class SessionDetail(BaseModel):
    session_id: str
    project: str
    started: datetime
    ended: datetime
    timeline: list[TimelineEntry]
    context: ContextSummary
    cache: SessionCache
    loops: list[LoopFinding]
    # Commits in the session's git repository (a rough outcome signal); None outside git.
    outcome: SessionOutcome | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cost(self) -> float:
        return sum(e.cost.total for e in self.timeline if e.cost is not None)


def session_detail(
    records: list[RequestRecord], calls: list[ToolCall], pricing: Pricing
) -> SessionDetail:
    """Detail of one session's records (non-empty) and tool calls."""
    records = sorted(records, key=lambda r: r.timestamp)
    cache = session_misses(records, pricing)
    causes = {m.message_id: m.cause for m in cache.misses}
    timeline = []
    for record in records:
        window = pricing.context_window(record.model)
        timeline.append(
            TimelineEntry(
                message_id=record.message_id,
                timestamp=record.timestamp,
                model=record.model,
                usage=record.usage,
                cost=pricing.cost(record.model, record.usage, record.speed),
                tool_names=record.tool_names,
                is_sidechain=record.is_sidechain,
                stop_reason=record.stop_reason,
                ttft_ms=record.ttft_ms,
                latency_ms=record.latency_ms,
                context_share=record.usage.prompt_tokens / window if window else None,
                miss_cause=causes.get(record.message_id),
            )
        )
    return SessionDetail(
        session_id=records[0].session_id,
        project=records[0].project,
        started=records[0].timestamp,
        ended=records[-1].timestamp,
        timeline=timeline,
        context=_context(timeline, cache),
        cache=cache,
        loops=detect_loops(calls),
    )


def _context(timeline: list[TimelineEntry], cache: SessionCache) -> ContextSummary:
    main = [e for e in timeline if not e.is_sidechain] or timeline
    prompts = [e.usage.prompt_tokens for e in main]
    peak = max(timeline, key=lambda e: e.usage.prompt_tokens)
    steps = [b - a for a, b in pairwise(prompts)]
    return ContextSummary(
        peak_prompt_tokens=peak.usage.prompt_tokens,
        peak_share=peak.context_share,
        final_prompt_tokens=prompts[-1],
        average_growth=sum(steps) / len(steps) if steps else 0.0,
        resets=cache.counts["context_reset"],
    )
