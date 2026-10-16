"""Observe a Messages API response while it is forwarded unchanged.

`ResponseTee` gets every chunk the client gets, after the client gets it, and keeps only what a
record needs: message id, model, usage, stop reason, tool names and timings. Streamed (SSE)
responses are parsed incrementally, so the time to the first content block is known; JSON
responses are buffered and parsed by `observation()`, which runs on the recorder thread.

Nothing here may affect the response: any parsing error is logged once and turns the tee off.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import structlog
from pydantic import BaseModel, Field

from claude_lens.importers.claude_code import parse_usage
from claude_lens.models import Speed, Usage

log = structlog.get_logger(__name__)

Clock = Callable[[], float]


class ToolUse(BaseModel):
    """A tool_use block of the response; its input is hashed by the recorder, never stored."""

    id: str
    name: str
    input: Any = None


class ResponseObservation(BaseModel):
    """What the proxy learned about one response; no content."""

    message_id: str | None = None
    model: str | None = None
    usage: Usage = Field(default_factory=Usage)
    speed: Speed = "standard"
    stop_reason: str | None = None
    tool_names: list[str] = Field(default_factory=list)
    tool_uses: list[ToolUse] = Field(default_factory=list)
    thinking_tokens: int | None = None
    ttft_ms: float | None = None
    latency_ms: float | None = None


class SSEParser:
    """Incremental server-sent events parser: feed bytes, get (event, data) pairs."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[str]:
        """Data fields of the events completed by `chunk`."""
        self._buffer += chunk.replace(b"\r\n", b"\n")
        events: list[str] = []
        while b"\n\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n\n", 1)
            data = _event_data(raw)
            if data is not None:
                events.append(data)
        return events

    def flush(self) -> list[str]:
        raw, self._buffer = self._buffer, b""
        data = _event_data(raw)
        return [data] if data is not None else []


def _event_data(raw: bytes) -> str | None:
    lines = [
        line[5:].removeprefix(" ")
        for line in raw.decode("utf-8", errors="replace").split("\n")
        if line.startswith("data:")
    ]
    return "\n".join(lines) if lines else None


class ResponseTee:
    def __init__(self, *, sse: bool, started: float, clock: Clock) -> None:
        self.sse = sse
        self._started = started
        self._clock = clock
        self._parser = SSEParser()
        self._body: list[bytes] = []
        self._message: dict[str, Any] = {}
        self._usage: dict[str, Any] = {}
        self._tools: list[str] = []
        # Streamed tool_use blocks by content index: id, name and input JSON fragments.
        self._tool_blocks: dict[int, tuple[str, str, list[str]]] = {}
        self._tool_uses: list[ToolUse] = []
        self._ttft: float | None = None
        self._latency: float | None = None
        self._broken = False

    def feed(self, chunk: bytes) -> None:
        if self._broken:
            return
        try:
            if self.sse:
                for data in self._parser.feed(chunk):
                    self._event(data)
            else:
                self._body.append(chunk)
        except Exception as exc:  # fail open: the response must not notice
            self._fail(exc)

    def close(self) -> None:
        """The stream ended (or broke): fix the total latency."""
        if self._latency is not None:
            return
        self._latency = self._elapsed()
        if self.sse and not self._broken:
            try:
                for data in self._parser.flush():
                    self._event(data)
            except Exception as exc:
                self._fail(exc)

    def observation(self) -> ResponseObservation | None:
        """The parsed response; None when it carried no message id or the tee broke."""
        if self._broken:
            return None
        try:
            if not self.sse:
                body = json.loads(b"".join(self._body))
                if not isinstance(body, dict) or body.get("type") != "message":
                    return None
                self._message = body
                usage = body.get("usage")
                self._usage = usage if isinstance(usage, dict) else {}
                self._tools = _tool_names(body.get("content"))
                self._tool_uses = _tool_uses(body.get("content"))
            else:
                self._tool_uses = [
                    ToolUse(id=tool_id, name=name, input=_parse_input(parts))
                    for _, (tool_id, name, parts) in sorted(self._tool_blocks.items())
                ]
            return self._observation()
        except Exception as exc:
            self._fail(exc)
            return None

    def _observation(self) -> ResponseObservation | None:
        message_id = self._message.get("id")
        if not isinstance(message_id, str):
            return None
        model = self._message.get("model")
        stop_reason = self._message.get("stop_reason")
        return ResponseObservation(
            message_id=message_id,
            model=model if isinstance(model, str) else None,
            usage=parse_usage(self._usage),
            speed="fast" if self._usage.get("speed") == "fast" else "standard",
            stop_reason=stop_reason if isinstance(stop_reason, str) else None,
            tool_names=self._tools,
            tool_uses=self._tool_uses,
            thinking_tokens=_thinking_tokens(self._usage),
            ttft_ms=self._ttft,
            latency_ms=self._latency,
        )

    def _event(self, data: str) -> None:
        try:
            event = json.loads(data)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "message_start" and isinstance(event.get("message"), dict):
            self._message = dict(event["message"])
            usage = self._message.get("usage")
            self._usage = dict(usage) if isinstance(usage, dict) else {}
        elif kind == "content_block_start":
            if self._ttft is None:
                self._ttft = self._elapsed()
            block = event.get("content_block")
            if isinstance(block, dict):
                self._tools = _add_tool(self._tools, block)
                index, tool_id, name = event.get("index"), block.get("id"), block.get("name")
                if (
                    block.get("type") == "tool_use"
                    and isinstance(index, int)
                    and isinstance(tool_id, str)
                    and isinstance(name, str)
                ):
                    self._tool_blocks[index] = (tool_id, name, [])
        elif kind == "content_block_delta":
            delta, index = event.get("delta"), event.get("index")
            if (
                isinstance(delta, dict)
                and delta.get("type") == "input_json_delta"
                and isinstance(delta.get("partial_json"), str)
                and index in self._tool_blocks
            ):
                self._tool_blocks[index][2].append(delta["partial_json"])
        elif kind == "message_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                self._message["stop_reason"] = delta["stop_reason"]
            usage = event.get("usage")
            if isinstance(usage, dict):
                # Final counts: output always, input and cache fields on newer API versions.
                self._usage.update({k: v for k, v in usage.items() if v is not None})

    def _elapsed(self) -> float:
        return round((self._clock() - self._started) * 1000, 1)

    def _fail(self, exc: Exception) -> None:
        self._broken = True
        log.warning("tee_parse_failed", error_type=type(exc).__name__)


def _add_tool(names: list[str], block: dict[str, Any]) -> list[str]:
    name = block.get("name")
    if block.get("type") == "tool_use" and isinstance(name, str) and name not in names:
        return [*names, name]
    return names


def _tool_names(content: Any) -> list[str]:
    names: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                names = _add_tool(names, block)
    return names


def _tool_uses(content: Any) -> list[ToolUse]:
    uses: list[ToolUse] = []
    for block in content if isinstance(content, list) else []:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and isinstance(block.get("id"), str)
            and isinstance(block.get("name"), str)
        ):
            uses.append(ToolUse(id=block["id"], name=block["name"], input=block.get("input")))
    return uses


def _parse_input(parts: list[str]) -> Any:
    """Input of a streamed tool_use block; an empty stream means `{}`, broken JSON None."""
    text = "".join(parts)
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return None


def _thinking_tokens(usage: dict[str, Any]) -> int | None:
    details = usage.get("output_tokens_details")
    value = details.get("thinking_tokens") if isinstance(details, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
