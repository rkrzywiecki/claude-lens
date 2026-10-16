"""Write proxy records off the request path.

One worker thread owns the SQLite connection and the session tracker, so neither needs a lock
and no request ever waits for the database. Request bodies are parsed there too (only to group
sessions; nothing of them is stored). Every failure is logged and counted, never raised: a
broken or locked database costs records, not responses.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from claude_lens.fingerprint import segment_fingerprints, tool_input_hash, tool_target
from claude_lens.models import RequestFeatures, RequestRecord, ToolCall
from claude_lens.proxy.sessions import SessionTracker
from claude_lens.proxy.tee import ResponseTee
from claude_lens.store import Store

log = structlog.get_logger(__name__)


class RecordJob(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    started_at: datetime
    request_body: bytes
    request_id: str | None = None
    session_header: str | None = None
    project_header: str | None = None
    tee: ResponseTee


class RecordBatch(BaseModel):
    record: RequestRecord
    tool_calls: list[ToolCall]
    # (tool_use id, is_error) of the results the request sends back.
    tool_results: list[tuple[str, bool]]


def tool_results(body: dict[str, Any]) -> list[tuple[str, bool]]:
    """Results in the newest message: those answer the previous response's tool calls."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
        return []
    content = messages[-1].get("content")
    blocks = content if isinstance(content, list) else []
    return [
        (block["tool_use_id"], block.get("is_error") is True)
        for block in blocks
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and isinstance(block.get("tool_use_id"), str)
    ]


def request_effort(body: dict[str, Any]) -> str | None:
    config = body.get("output_config")
    effort = config.get("effort") if isinstance(config, dict) else None
    return effort if isinstance(effort, str) else None


def request_features(body: dict[str, Any]) -> RequestFeatures:
    """Cost-relevant settings of a request body; sizes and flags only, no content."""
    tools = body.get("tools")
    thinking = body.get("thinking")
    max_tokens = body.get("max_tokens")
    system = body.get("system")
    if isinstance(system, str):
        system_chars = len(system)
    elif isinstance(system, list):
        system_chars = sum(
            len(b["text"]) for b in system if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    else:
        system_chars = 0
    return RequestFeatures(
        stream=body.get("stream") is True,
        max_tokens=max_tokens if isinstance(max_tokens, int) else None,
        tool_count=len(tools) if isinstance(tools, list) else 0,
        cache_control=_has_cache_control(body),
        thinking=thinking.get("type") if isinstance(thinking, dict) else None,
        system_chars=system_chars,
    )


def _has_cache_control(value: Any) -> bool:
    if isinstance(value, dict):
        return "cache_control" in value or any(_has_cache_control(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_cache_control(v) for v in value)
    return False


class Recorder:
    def __init__(
        self, open_store: Callable[[], Store], tracker: SessionTracker | None = None
    ) -> None:
        self._open_store = open_store
        self._store: Store | None = None
        self._tracker = tracker or SessionTracker()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lens-recorder")
        self.recorded = 0
        self.skipped = 0
        self.errors = 0

    def submit(self, job: RecordJob) -> None:
        try:
            self._executor.submit(self._write, job)
        except RuntimeError:  # shutting down
            self.errors += 1
            log.warning("record_dropped", reason="recorder closed")

    def drain(self) -> None:
        """Wait until every job submitted so far is written (tests, shutdown)."""
        self._executor.submit(lambda: None).result()

    def close(self) -> None:
        self._executor.submit(self._close_store)
        self._executor.shutdown(wait=True)

    def build(self, job: RecordJob) -> RecordBatch | None:
        observation = job.tee.observation()
        if observation is None or observation.message_id is None or observation.model is None:
            return None
        try:
            body = json.loads(job.request_body)
        except ValueError:
            body = None
        session = self._tracker.assign(body, job.session_header, job.project_header)
        calls = [
            ToolCall(
                tool_use_id=use.id,
                message_id=observation.message_id,
                session_id=session.session_id,
                timestamp=job.started_at,
                tool_name=use.name,
                input_hash=tool_input_hash(use.input),
                target=tool_target(use.input),
                source="proxy",
            )
            for use in observation.tool_uses
        ]
        record = RequestRecord(
            message_id=observation.message_id,
            request_id=job.request_id,
            session_id=session.session_id,
            project=session.project,
            model=observation.model,
            timestamp=job.started_at,
            usage=observation.usage,
            speed=observation.speed,
            stop_reason=observation.stop_reason,
            tool_names=observation.tool_names,
            source="proxy",
            ttft_ms=observation.ttft_ms,
            latency_ms=observation.latency_ms,
            segments=segment_fingerprints(body) if isinstance(body, dict) else None,
            effort=request_effort(body) if isinstance(body, dict) else None,
            thinking_tokens=observation.thinking_tokens,
            features=request_features(body) if isinstance(body, dict) else None,
        )
        results = tool_results(body) if isinstance(body, dict) else []
        return RecordBatch(record=record, tool_calls=calls, tool_results=results)

    def _write(self, job: RecordJob) -> None:
        try:
            batch = self.build(job)
            if batch is None:
                self.skipped += 1
                return
            if self._store is None:
                self._store = self._open_store()
            self._store.upsert([batch.record])
            self._store.upsert_tool_calls(batch.tool_calls)
            self._store.mark_tool_results(batch.tool_results)
            self.recorded += 1
        except Exception as exc:  # fail open: never let a record break the worker
            self.errors += 1
            log.error("record_failed", error_type=type(exc).__name__, error=str(exc))

    def _close_store(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None
