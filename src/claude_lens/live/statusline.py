"""`claude-lens statusline`: one line for Claude Code's status line.

Claude Code runs the command after each assistant message (and on a timer when
`refreshInterval` is set) with session data as JSON on stdin; see
https://code.claude.com/docs/en/statusline. The line shows the context size and its share of
the model's window, the session's API-equivalent cost so far, and whether the prompt cache is
still warm, with the time left on its TTL.

Context, cost and cache come from the session transcript (`transcript_path`), read
incrementally. When the transcript cannot be read, Claude Code's own numbers from stdin are used
instead. The command never fails: the worst case is a shorter line.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from claude_lens.insights.thresholds import KITCHEN_SINK_PEAK_TOKENS, NEAR_LIMIT_SHARE
from claude_lens.live.session import ONE_HOUR, LiveSession, SessionSnapshot

SEPARATOR = " · "
GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ModelInfo(_Lenient):
    id: str | None = None
    display_name: str | None = None


class WindowInfo(_Lenient):
    context_window_size: int | None = None
    total_input_tokens: int | None = None


class CostInfo(_Lenient):
    total_cost_usd: float | None = None


class CacheInfo(_Lenient):
    warm: bool | None = None
    ttl: str | None = None
    expires_at: float | None = None


class StatusInput(_Lenient):
    """The fields of Claude Code's status line JSON this command uses."""

    session_id: str | None = None
    transcript_path: str | None = None
    model: ModelInfo | None = None
    context_window: WindowInfo | None = None
    cost: CostInfo | None = None
    prompt_cache: CacheInfo | None = None


def build_line(
    data: StatusInput, snapshot: SessionSnapshot | None, now: float, *, color: bool
) -> str:
    """The status line for one run; `snapshot` is None when the transcript was unreadable."""

    def paint(text: str, code: str | None) -> str:
        return f"{code}{text}{RESET}" if color and code else text

    parts: list[str] = []
    name = data.model.display_name if data.model and data.model.display_name else None
    if name or (snapshot and snapshot.model):
        parts.append(name or short_model(snapshot.model if snapshot else None))

    window = data.context_window.context_window_size if data.context_window else None
    if snapshot is not None:
        tokens = snapshot.context_tokens
        window = window or snapshot.context_window
    else:
        tokens = (data.context_window.total_input_tokens or 0) if data.context_window else 0
    if tokens or window:
        text = f"ctx {tokens_label(tokens)}"
        share = tokens / window if window else None
        if window and share is not None:
            text += f"/{tokens_label(window)} ({share:.0%})"
        level = None
        if share is not None and share >= NEAR_LIMIT_SHARE:
            level = RED
        elif tokens >= KITCHEN_SINK_PEAK_TOKENS:
            level = YELLOW
        parts.append(paint(text, level))

    if snapshot is not None and snapshot.requests:
        cost = f"${snapshot.cost_usd:,.2f}"
        if snapshot.unpriced_requests:
            cost += "+"
        parts.append(cost)
    elif data.cost and data.cost.total_cost_usd is not None:
        parts.append(f"${data.cost.total_cost_usd:,.2f}")

    cache = cache_label(data.prompt_cache, snapshot, now)
    if cache is not None:
        text, warm = cache
        parts.append(paint(text, GREEN if warm else DIM))
    return SEPARATOR.join(parts) if parts else "claude-lens"


def cache_label(
    info: CacheInfo | None, snapshot: SessionSnapshot | None, now: float
) -> tuple[str, bool] | None:
    """('cache warm 3m left (5m)', True), or None when the session never used the cache.

    Claude Code's own `prompt_cache` (v2.1.251+) wins when present: it knows the TTL of the
    current prefix. Otherwise the TTL is 1 hour when the session wrote any 1-hour cache.
    """
    if info is not None and info.warm is not None:
        ttl = info.ttl or "5m"
        if info.warm and info.expires_at is not None:
            return f"cache warm {duration_label(info.expires_at - now)} left ({ttl})", True
        return f"cache cold ({ttl})", False
    if snapshot is None or snapshot.last_request_at is None:
        return None
    ttl = "1h" if snapshot.cache_ttl_seconds == ONE_HOUR else "5m"
    left = snapshot.cache_seconds_left(now)
    if left is None:
        return None
    if left > 0:
        return f"cache warm {duration_label(left)} left ({ttl})", True
    return f"cache cold ({ttl})", False


def short_model(model: str | None) -> str:
    """claude-opus-5-5 -> opus-5-5."""
    if not model:
        return "?"
    return model.removeprefix("claude-")


def tokens_label(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M".replace(".0M", "M")
    if tokens >= 1000:
        return f"{tokens // 1000}k"
    return str(tokens)


def duration_label(seconds: float) -> str:
    if seconds < 60:
        return "<1m"
    minutes = int(seconds // 60)
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"


def render(stdin_text: str, now: float | None = None, *, color: bool | None = None) -> str:
    """Parse Claude Code's JSON, read the transcript and return the line. Never raises."""
    now = time.time() if now is None else now
    color = (not os.environ.get("NO_COLOR")) if color is None else color
    try:
        data = StatusInput.model_validate(json.loads(stdin_text or "{}"))
    except (ValueError, ValidationError):
        data = StatusInput()
    snapshot = None
    if data.transcript_path and Path(data.transcript_path).is_file():
        try:
            session = LiveSession.open(Path(data.transcript_path))
            if data.context_window:
                session.remember_window(data.context_window.context_window_size)
            snapshot = session.snapshot()
            session.save()
        except Exception:  # a status line must never break the UI
            snapshot = None
    try:
        return build_line(data, snapshot, now, color=color)
    except Exception:
        return "claude-lens"


def main() -> int:
    print(render(sys.stdin.read()))
    return 0
