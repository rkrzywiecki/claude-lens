"""Parsing of single Claude Code transcript entries into request records.

Kept free of the store and logging so that the live commands (`statusline`, `hook`), which must
start in well under 200 ms, can share it with the importer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from claude_lens.models import RequestRecord, Speed, Usage

# Placeholder model Claude Code writes for locally generated messages (no API call happened).
SYNTHETIC_MODEL = "<synthetic>"


def parse_usage(raw: dict[str, Any]) -> Usage:
    """Map an API `usage` object to Usage; cache writes without a TTL split count as 5-minute."""
    cache_write = _int(raw.get("cache_creation_input_tokens"))
    split = raw.get("cache_creation")
    if isinstance(split, dict):
        write_1h = _int(split.get("ephemeral_1h_input_tokens"))
        write_5m = _int(split.get("ephemeral_5m_input_tokens"))
        if write_1h + write_5m != cache_write:
            write_5m = max(cache_write - write_1h, 0)
    else:
        write_1h, write_5m = 0, cache_write
    return Usage(
        input_tokens=_int(raw.get("input_tokens")),
        output_tokens=_int(raw.get("output_tokens")),
        cache_read_tokens=_int(raw.get("cache_read_input_tokens")),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )


def parse_record(entry: Any, fallback_project: str) -> RequestRecord | None:
    """The request record of an `assistant` entry; None for anything else."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    model = message.get("model")
    usage = message.get("usage")
    session_id = entry.get("sessionId")
    timestamp = parse_timestamp(entry.get("timestamp"))
    if (
        not isinstance(message_id, str)
        or not isinstance(model, str)
        or model == SYNTHETIC_MODEL
        or not isinstance(usage, dict)
        or not isinstance(session_id, str)
        or timestamp is None
    ):
        return None
    cwd = entry.get("cwd")
    request_id = entry.get("requestId")
    stop_reason = message.get("stop_reason")
    effort = entry.get("effort")
    agent_id = entry.get("agentId")
    details = usage.get("output_tokens_details")
    thinking = details.get("thinking_tokens") if isinstance(details, dict) else None
    return RequestRecord(
        message_id=message_id,
        request_id=request_id if isinstance(request_id, str) else None,
        session_id=session_id,
        project=cwd if isinstance(cwd, str) and cwd else fallback_project,
        model=model,
        timestamp=timestamp,
        usage=parse_usage(usage),
        speed=_speed(usage.get("speed")),
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        tool_names=_tool_names(message.get("content")),
        is_sidechain=entry.get("isSidechain") is True,
        source="claude_code",
        effort=effort if isinstance(effort, str) else None,
        thinking_tokens=_int(thinking) if thinking is not None else None,
        agent_id=agent_id if isinstance(agent_id, str) else None,
    )


def merge_records(previous: RequestRecord, current: RequestRecord) -> RequestRecord:
    """Fold two entries of one API response (one per content block) into one record."""
    base = current if current.usage.output_tokens >= previous.usage.output_tokens else previous
    tools = previous.tool_names + [t for t in current.tool_names if t not in previous.tool_names]
    return base.model_copy(
        update={
            "tool_names": tools,
            "timestamp": min(previous.timestamp, current.timestamp),
            "stop_reason": current.stop_reason or previous.stop_reason,
        }
    )


def is_human_prompt(entry: dict[str, Any]) -> bool:
    """A prompt the user typed, as opposed to tool results and injected context."""
    origin = entry.get("origin")
    if isinstance(origin, dict) and "kind" in origin:
        return origin.get("kind") == "human"
    if entry.get("isMeta") or entry.get("toolUseResult") is not None:
        return False
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _tool_names(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    names: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name not in names:
                names.append(name)
    return names


def _speed(value: Any) -> Speed:
    return "fast" if value == "fast" else "standard"


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
