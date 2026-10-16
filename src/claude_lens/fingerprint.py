"""Content-free fingerprints of requests and tool calls.

Only short hashes leave this module, never the content they were computed from.

A request's prompt is cached as one prefix, in this order: tool definitions, system prompt,
then messages. Settings such as `thinking` or `tool_choice` are not part of the text but still
invalidate the cached messages when they change. `segment_fingerprints` hashes each part on its
own (not chained), so two requests can be compared position by position to find the first part
that changed: everything from there on had to be written to the cache again.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Positions of the fixed segments; messages follow from MESSAGES_START on.
TOOLS, SYSTEM, PARAMS = 0, 1, 2
MESSAGES_START = 3

# Request fields that change how cached messages are interpreted, not the prompt text.
_CACHE_PARAMS = ("tool_choice", "thinking")

# Tool inputs that name the file a tool reads or changes (Claude Code and the text editor tool).
_TARGET_KEYS = ("file_path", "notebook_path", "path")

_HASH_CHARS = 12


def canonical(value: Any) -> bytes:
    """Stable JSON bytes: sorted keys, no whitespace, `cache_control` markers removed."""
    return json.dumps(
        _strip_cache_control(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def normalize_message(message: Any) -> Any:
    """`"content": "text"` and a single text block are the same message."""
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return {**message, "content": [{"type": "text", "text": message["content"]}]}
    return message


def short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:_HASH_CHARS]


def segment_fingerprints(body: dict[str, Any]) -> list[str] | None:
    """[tools, system, params, message 0, message 1, ...] hashes; None without messages."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    params = {key: body[key] for key in _CACHE_PARAMS if key in body}
    output_config = body.get("output_config")
    if isinstance(output_config, dict) and "effort" in output_config:
        params["effort"] = output_config["effort"]
    fixed = [
        short_hash(canonical(body.get("tools"))),
        short_hash(canonical(normalize_message({"content": body.get("system")}))),
        short_hash(canonical(params)),
    ]
    return fixed + [short_hash(canonical(normalize_message(m))) for m in messages]


def first_difference(previous: list[str], current: list[str]) -> int | None:
    """Index of the first segment that differs; None when one is a prefix of the other."""
    for index, (old, new) in enumerate(zip(previous, current, strict=False)):
        if old != new:
            return index
    return None


def describe_segment(index: int) -> str:
    if index == TOOLS:
        return "tool definitions changed"
    if index == SYSTEM:
        return "system prompt changed"
    if index == PARAMS:
        return "thinking, effort or tool_choice changed"
    return f"message {index - MESSAGES_START + 1} of the history changed"


def tool_input_hash(tool_input: Any) -> str:
    return short_hash(canonical(tool_input))


def tool_target(tool_input: Any) -> str | None:
    """The file a tool call works on, if its input names one."""
    if not isinstance(tool_input, dict):
        return None
    for key in _TARGET_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _strip_cache_control(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_cache_control(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_strip_cache_control(v) for v in value]
    return value
