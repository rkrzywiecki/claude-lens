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

from claude_lens.models import ImportStats, RequestRecord, Speed, Usage
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


def parse_transcript(
    path: Path, stats: ImportStats | None = None, fallback_project: str | None = None
) -> list[RequestRecord]:
    """Request records of one transcript file, one per API response.

    `fallback_project` names the project when records carry no `cwd`; it defaults to the name
    of the directory holding the file.
    """
    stats = stats if stats is not None else ImportStats()
    merged: dict[str, RequestRecord] = {}
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
            record = _record(entry, fallback_project)
            if record is None:
                continue
            previous = merged.get(record.message_id)
            if previous is not None:
                record = _merge(previous, record)
            merged[record.message_id] = record
    return list(merged.values())


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
            records = parse_transcript(path, stats, _project_dir(root, path))
        except OSError as exc:
            log.warning("transcript_unreadable", path=str(path), error=str(exc))
            stats.files_skipped += 1
            continue
        stats.records += store.upsert(records)
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
    )


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
