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

import structlog
from pydantic import BaseModel, ConfigDict

from claude_lens.models import RequestRecord
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

    def build(self, job: RecordJob) -> RequestRecord | None:
        observation = job.tee.observation()
        if observation is None or observation.message_id is None or observation.model is None:
            return None
        try:
            body = json.loads(job.request_body)
        except ValueError:
            body = None
        session = self._tracker.assign(body, job.session_header, job.project_header)
        return RequestRecord(
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
        )

    def _write(self, job: RecordJob) -> None:
        try:
            record = self.build(job)
            if record is None:
                self.skipped += 1
                return
            if self._store is None:
                self._store = self._open_store()
            self._store.upsert([record])
            self.recorded += 1
        except Exception as exc:  # fail open: never let a record break the worker
            self.errors += 1
            log.error("record_failed", error_type=type(exc).__name__, error=str(exc))

    def _close_store(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None
