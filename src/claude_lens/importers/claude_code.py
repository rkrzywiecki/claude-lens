"""Import Claude Code session transcripts (~/.claude/projects/**/*.jsonl).

The transcript format is not a stable API, so parsing is deliberately tolerant: unknown record
types are skipped, malformed lines are counted, and a missing field never aborts an import.

One API response may be written as several `assistant` records (one per content block) that share
`message.id` and repeat `usage`. Records are therefore merged per message id: tool names are
collected from every block, and usage is taken from the record with the most output tokens (a
streamed message can be logged before its final usage is known).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

from claude_lens.fingerprint import tool_input_hash, tool_target
from claude_lens.models import ImportStats, RequestRecord, SessionMeta, Speed, ToolCall, Usage
from claude_lens.store import Store

log = structlog.get_logger(__name__)

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


class TranscriptData(BaseModel):
    records: list[RequestRecord] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # tool_use id -> whether its tool_result was an error.
    tool_results: dict[str, bool] = Field(default_factory=dict)
    sessions: list[SessionMeta] = Field(default_factory=list)


def parse_transcript(
    path: Path, stats: ImportStats | None = None, fallback_project: str | None = None
) -> list[RequestRecord]:
    """Request records of one transcript file, one per API response."""
    return parse_transcript_data(path, stats, fallback_project).records


def parse_transcript_data(
    path: Path, stats: ImportStats | None = None, fallback_project: str | None = None
) -> TranscriptData:
    """Request records, tool calls and tool results of one transcript file.

    `fallback_project` names the project when records carry no `cwd`; it defaults to the name
    of the directory holding the file.
    """
    stats = stats if stats is not None else ImportStats()
    merged: dict[str, RequestRecord] = {}
    calls: dict[str, ToolCall] = {}
    results: dict[str, bool] = {}
    metas: dict[str, SessionMeta] = {}
    fallback_project = fallback_project or path.parent.name
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                stats.bad_lines += 1
                continue
            results.update(_tool_results(entry))
            _observe_session(metas, entry, str(path))
            record = _record(entry, fallback_project)
            if record is None:
                continue
            for call in _tool_calls(entry, record):
                calls[call.tool_use_id] = call
            previous = merged.get(record.message_id)
            if previous is not None:
                record = _merge(previous, record)
            merged[record.message_id] = record
    for tool_use_id, call in calls.items():
        if tool_use_id in results:
            calls[tool_use_id] = call.model_copy(update={"is_error": results[tool_use_id]})
    return TranscriptData(
        records=list(merged.values()),
        tool_calls=list(calls.values()),
        tool_results=results,
        sessions=list(metas.values()),
    )


def find_transcripts(root: Path) -> Iterator[Path]:
    """Session and subagent transcripts under `root`, in a stable order."""
    yield from sorted(root.rglob("*.jsonl"))


def import_transcripts(store: Store, root: Path, *, full: bool = False) -> ImportStats:
    """Import every transcript under `root`; unchanged files are skipped unless `full`."""
    stats = ImportStats()
    for path in find_transcripts(root):
        stats.files_seen += 1
        try:
            st = path.stat()
        except OSError:
            stats.files_skipped += 1
            continue
        if not full and store.is_unchanged(path, st.st_size, st.st_mtime):
            stats.files_skipped += 1
            continue
        try:
            data = parse_transcript_data(path, stats, _project_dir(root, path))
        except OSError as exc:
            log.warning("transcript_unreadable", path=str(path), error=str(exc))
            stats.files_skipped += 1
            continue
        stats.records += store.upsert(data.records)
        stats.tool_calls += store.upsert_tool_calls(data.tool_calls)
        # Results whose call was logged in another file (a resumed or forked session).
        store.mark_tool_results(data.tool_results.items())
        store.upsert_session_meta(data.sessions)
        store.mark_imported(path, st.st_size, st.st_mtime)
        stats.files_imported += 1
    return stats


def _record(entry: Any, fallback_project: str) -> RequestRecord | None:
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    model = message.get("model")
    usage = message.get("usage")
    session_id = entry.get("sessionId")
    timestamp = _timestamp(entry.get("timestamp"))
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


def _observe_session(metas: dict[str, SessionMeta], entry: Any, source_file: str) -> None:
    """Fold one transcript entry into its session's metadata."""
    if not isinstance(entry, dict) or not isinstance(entry.get("sessionId"), str):
        return
    session_id = entry["sessionId"]
    meta = metas.setdefault(session_id, SessionMeta(session_id=session_id, source_file=source_file))
    for field, key in (("entrypoint", "entrypoint"), ("version", "version")):
        value = entry.get(key)
        if isinstance(value, str) and value and getattr(meta, field) is None:
            setattr(meta, field, value)
    kind = entry.get("type")
    if kind == "user":
        mode = entry.get("permissionMode")
        if isinstance(mode, str) and mode not in meta.permission_modes:
            meta.permission_modes.append(mode)
        if _is_human_prompt(entry):
            meta.human_prompts += 1
        if entry.get("interruptedMessageId"):
            meta.interruptions += 1
        if entry.get("toolDenialKind"):
            meta.tool_denials += 1
    elif kind == "assistant":
        if entry.get("isApiErrorMessage") is True:
            meta.api_errors += 1
        for field, key in (("skills", "attributionSkill"), ("agents", "attributionAgent")):
            value = entry.get(key)
            names = getattr(meta, field)
            if isinstance(value, str) and value and value not in names:
                names.append(value)
    elif kind == "system" and entry.get("subtype") == "turn_duration":
        meta.active_ms += _int(entry.get("durationMs"))


def _is_human_prompt(entry: dict[str, Any]) -> bool:
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


def _tool_calls(entry: dict[str, Any], record: RequestRecord) -> list[ToolCall]:
    content = entry["message"].get("content")
    calls: list[ToolCall] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_use_id, name = block.get("id"), block.get("name")
        if not isinstance(tool_use_id, str) or not isinstance(name, str):
            continue
        tool_input = block.get("input")
        calls.append(
            ToolCall(
                tool_use_id=tool_use_id,
                message_id=record.message_id,
                session_id=record.session_id,
                timestamp=record.timestamp,
                tool_name=name,
                input_hash=tool_input_hash(tool_input),
                target=tool_target(tool_input),
            )
        )
    return calls


def _tool_results(entry: Any) -> dict[str, bool]:
    """tool_use id -> is_error, from the tool_result blocks of a user record."""
    if not isinstance(entry, dict) or entry.get("type") != "user":
        return {}
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    results: dict[str, bool] = {}
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            tool_use_id = block.get("tool_use_id")
            if isinstance(tool_use_id, str):
                results[tool_use_id] = block.get("is_error") is True
    return results


def _merge(previous: RequestRecord, current: RequestRecord) -> RequestRecord:
    base = current if current.usage.output_tokens >= previous.usage.output_tokens else previous
    tools = previous.tool_names + [t for t in current.tool_names if t not in previous.tool_names]
    return base.model_copy(
        update={
            "tool_names": tools,
            "timestamp": min(previous.timestamp, current.timestamp),
            "stop_reason": current.stop_reason or previous.stop_reason,
        }
    )


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


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _speed(value: Any) -> Speed:
    return "fast" if value == "fast" else "standard"


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _project_dir(root: Path, path: Path) -> str:
    """Top-level directory under `root`: Claude Code names it after the cwd ('/' -> '-')."""
    parts = path.relative_to(root).parts
    return parts[0] if len(parts) > 1 else root.name
