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
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

from claude_lens.fingerprint import tool_input_hash, tool_target
from claude_lens.importers.records import (
    SYNTHETIC_MODEL,
    is_human_prompt,
    merge_records,
    parse_record,
    parse_usage,
)
from claude_lens.models import ImportStats, RequestRecord, SessionMeta, ToolCall
from claude_lens.store import Store

__all__ = [
    "SYNTHETIC_MODEL",
    "TranscriptData",
    "find_transcripts",
    "import_transcripts",
    "parse_transcript",
    "parse_transcript_data",
    "parse_usage",
]

log = structlog.get_logger(__name__)


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
            record = parse_record(entry, fallback_project)
            if record is None:
                continue
            for call in _tool_calls(entry, record):
                calls[call.tool_use_id] = call
            previous = merged.get(record.message_id)
            if previous is not None:
                record = merge_records(previous, record)
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
        if is_human_prompt(entry):
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


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _project_dir(root: Path, path: Path) -> str:
    """Top-level directory under `root`: Claude Code names it after the cwd ('/' -> '-')."""
    parts = path.relative_to(root).parts
    return parts[0] if len(parts) > 1 else root.name
