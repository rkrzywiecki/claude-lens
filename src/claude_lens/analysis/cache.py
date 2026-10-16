"""Cache doctor: find requests that missed the prompt cache and say why.

Within one conversation, every request normally reads from the cache what the previous request
read or wrote, plus a little more. A request is a **miss** when it reads markedly less, so part of
the prefix had to be sent again: written to the cache at 1.25x or 2x the input price instead of
read at about 0.1x.

Requests are compared within a stream: same session, same model (caches are per model), main
conversation only (subagents interleave with their own prefixes). Each miss gets a cause:

- `context_reset`: the prompt shrank by more than half (compaction, `/clear`, a side call that
  shares the session). Expected, never counted as a penalty.
- `prefix_changed`: the proxy saw which prompt segment differs (tool definitions, system prompt,
  thinking/effort/tool_choice, or an earlier message) and the gap was within the cache TTL.
- `ttl_expired`: more time passed since the previous request than the cache TTL (5 minutes, or 1
  hour when the stream writes with the 1-hour TTL).
- `unexplained`: within the TTL and no visible change. From transcripts the prompt itself is not
  known, so most prefix changes land here; run through the proxy to pinpoint them.

The penalty of a miss is what re-sending the lost tokens cost minus what reading them would have
cost: at most the tokens the request actually wrote or sent uncached. A miss is **avoidable**
when a stable prefix or a longer TTL would have prevented it: a changed prefix, an unexplained
miss, or a 5-minute entry that expired during a pause of up to an hour. After longer pauses the
cache is gone whatever the client does.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, Field, computed_field

from claude_lens.fingerprint import describe_segment, first_difference
from claude_lens.models import RequestRecord
from claude_lens.pricing import Pricing, normalize_model

Cause = Literal["context_reset", "prefix_changed", "ttl_expired", "unexplained"]
CAUSES: tuple[Cause, ...] = ("prefix_changed", "ttl_expired", "unexplained", "context_reset")

TTL_5M = 300.0
TTL_1H = 3600.0
# A miss loses at least this share of the previous cached prefix, and this many tokens.
MIN_LOST_SHARE = 0.2
MIN_LOST_TOKENS = 1024
# A prompt that shrinks below this share of the previous one is a reset, not a miss.
RESET_SHARE = 0.5


class CacheMiss(BaseModel):
    message_id: str
    timestamp: datetime
    model: str
    gap_seconds: float
    ttl_seconds: float
    lost_tokens: int
    cause: Cause
    detail: str
    penalty: float
    avoidable: bool


class SessionCache(BaseModel):
    session_id: str
    project: str
    started: datetime
    requests: int
    cache_read_tokens: int
    prompt_tokens: int
    misses: list[CacheMiss] = Field(default_factory=list)
    uncached_requests: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hit_ratio(self) -> float | None:
        return self.cache_read_tokens / self.prompt_tokens if self.prompt_tokens else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def penalty(self) -> float:
        return sum(m.penalty for m in self.misses)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def avoidable_penalty(self) -> float:
        return sum(m.penalty for m in self.misses if m.avoidable)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = dict.fromkeys(CAUSES, 0)
        for miss in self.misses:
            counts[miss.cause] += 1
        return counts


class CacheDoctorReport(BaseModel):
    sessions: list[SessionCache]
    advice: list[str]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def penalty(self) -> float:
        return sum(s.penalty for s in self.sessions)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def avoidable_penalty(self) -> float:
        return sum(s.avoidable_penalty for s in self.sessions)


def diagnose(
    records: Iterable[RequestRecord], pricing: Pricing, top: int | None = None
) -> CacheDoctorReport:
    """Sessions ordered by avoidable penalty (highest first), with misses and advice."""
    by_session: dict[str, list[RequestRecord]] = defaultdict(list)
    for record in records:
        by_session[record.session_id].append(record)
    sessions = [_session(rs, pricing) for rs in by_session.values()]
    sessions.sort(key=lambda s: (-s.avoidable_penalty, -s.penalty, -s.requests, s.session_id))
    advice = _advice(sessions)
    return CacheDoctorReport(sessions=sessions[:top] if top else sessions, advice=advice)


def session_misses(records: list[RequestRecord], pricing: Pricing) -> SessionCache:
    """Misses of a single session's records."""
    return _session(records, pricing)


def _session(records: list[RequestRecord], pricing: Pricing) -> SessionCache:
    records = sorted(records, key=lambda r: r.timestamp)
    first = records[0]
    summary = SessionCache(
        session_id=first.session_id,
        project=first.project,
        started=first.timestamp,
        requests=len(records),
        cache_read_tokens=sum(r.usage.cache_read_tokens for r in records),
        prompt_tokens=sum(r.usage.prompt_tokens for r in records),
        uncached_requests=sum(
            1
            for r in records
            if r.usage.prompt_tokens >= MIN_LOST_TOKENS * 4
            and r.usage.cache_read_tokens == 0
            and r.usage.cache_write_tokens == 0
        ),
    )
    streams: dict[str, list[RequestRecord]] = defaultdict(list)
    for record in records:
        if not record.is_sidechain:
            streams[normalize_model(record.model)].append(record)
    for stream in streams.values():
        summary.misses.extend(_stream_misses(stream, pricing))
    summary.misses.sort(key=lambda m: m.timestamp)
    return summary


def _stream_misses(stream: list[RequestRecord], pricing: Pricing) -> list[CacheMiss]:
    misses: list[CacheMiss] = []
    ttl = TTL_5M
    for previous, current in pairwise(stream):
        if previous.usage.cache_write_1h_tokens:
            ttl = TTL_1H
        elif previous.usage.cache_write_5m_tokens:
            ttl = TTL_5M
        cached = previous.usage.cache_read_tokens + previous.usage.cache_write_tokens
        lost = cached - current.usage.cache_read_tokens
        if lost < MIN_LOST_TOKENS or lost < cached * MIN_LOST_SHARE:
            continue
        gap = (current.timestamp - previous.timestamp).total_seconds()
        cause, detail = _cause(previous, current, gap, ttl)
        misses.append(
            CacheMiss(
                message_id=current.message_id,
                timestamp=current.timestamp,
                model=current.model,
                gap_seconds=round(gap, 1),
                ttl_seconds=ttl,
                lost_tokens=lost,
                cause=cause,
                detail=detail,
                penalty=0.0 if cause == "context_reset" else _penalty(current, lost, pricing),
                avoidable=_avoidable(cause, gap, ttl),
            )
        )
    return misses


def _cause(
    previous: RequestRecord, current: RequestRecord, gap: float, ttl: float
) -> tuple[Cause, str]:
    if current.usage.prompt_tokens < previous.usage.prompt_tokens * RESET_SHARE:
        return "context_reset", (
            f"prompt shrank from {previous.usage.prompt_tokens:,} to "
            f"{current.usage.prompt_tokens:,} tokens"
        )
    if previous.segments is not None and current.segments is not None:
        index = first_difference(previous.segments, current.segments)
        if index is not None and gap <= ttl:
            return "prefix_changed", describe_segment(index)
    if gap > ttl:
        return "ttl_expired", f"{_duration(gap)} since the previous request, TTL {_duration(ttl)}"
    if previous.segments is not None and current.segments is not None:
        return "unexplained", "prompt prefix unchanged; the entry was evicted or not yet written"
    return "unexplained", "within the TTL; the transcript does not show what changed"


def _avoidable(cause: Cause, gap: float, ttl: float) -> bool:
    if cause == "context_reset":
        return False
    if cause == "ttl_expired":
        return ttl == TTL_5M and gap <= TTL_1H
    return True


def _penalty(current: RequestRecord, lost: int, pricing: Pricing) -> float:
    price = pricing.price_for(current.model)
    if price is None:
        return 0.0
    usage = current.usage
    tier = price.tier(usage.prompt_tokens)
    resent = min(lost, usage.cache_write_tokens + usage.input_tokens)
    if usage.cache_write_tokens:
        share_1h = usage.cache_write_1h_tokens / usage.cache_write_tokens
        multiplier = (
            share_1h * pricing.cache_write_1h_multiplier
            + (1 - share_1h) * pricing.cache_write_5m_multiplier
        )
    else:
        multiplier = 1.0
    scale = price.fast_multiplier if current.speed == "fast" else 1.0
    per_token = (tier.input * multiplier - tier.cache_read_price) * scale / 1_000_000
    return max(resent * per_token, 0.0)


def _advice(sessions: list[SessionCache]) -> list[str]:
    misses = [m for s in sessions for m in s.misses]
    advice: list[str] = []
    short_gaps = [
        m
        for m in misses
        if m.cause == "ttl_expired" and m.ttl_seconds == TTL_5M and m.gap_seconds <= TTL_1H
    ]
    if short_gaps:
        cost = sum(m.penalty for m in short_gaps)
        advice.append(
            f"{len(short_gaps)} misses came after pauses of 5-60 minutes (${cost:,.2f}). "
            "A 1-hour cache TTL, or a keep-alive request before the 5 minutes run out, "
            "would have kept these prefixes."
        )
    changed = [m for m in misses if m.cause == "prefix_changed"]
    if changed:
        by_detail: dict[str, int] = defaultdict(int)
        for miss in changed:
            by_detail[miss.detail] += 1
        worst = max(by_detail, key=lambda k: by_detail[k])
        advice.append(
            f"{len(changed)} misses within the TTL were caused by a changed prompt prefix; "
            f"most often: {worst} ({by_detail[worst]}x). Keep tools, system prompt and "
            "settings stable and put changing content after the last cache breakpoint."
        )
    unexplained = [m for m in misses if m.cause == "unexplained"]
    if unexplained:
        advice.append(
            f"{len(unexplained)} misses within the TTL have no visible cause in transcripts. "
            "Route the client through `claude-lens serve` to see which segment changed."
        )
    uncached = sum(s.uncached_requests for s in sessions)
    if uncached:
        advice.append(
            f"{uncached} large requests used no prompt cache at all; add cache_control "
            "breakpoints (or top-level automatic caching) to the client."
        )
    return advice


def _duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f} s"
