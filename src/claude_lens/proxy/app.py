"""FastAPI pass-through proxy for the Anthropic Messages API.

Every `/v1/*` request is forwarded to `LENS_UPSTREAM_URL` with its method, path, query, body and
headers (minus hop-by-hop headers and the `x-lens-*` grouping headers), and the upstream
response is streamed back with its status, headers and body unchanged, errors included.

Only successful `POST /v1/messages` responses are observed: a `ResponseTee` sees each chunk after
it is handed to the client, and the record is written by the `Recorder` thread once the stream
ends. Recording fails open: the client gets the same bytes whatever happens to the record.

Header values are never logged.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from claude_lens.config import Settings, get_settings
from claude_lens.proxy.recorder import Recorder, RecordJob
from claude_lens.proxy.tee import Clock, ResponseTee
from claude_lens.store import Store

log = structlog.get_logger(__name__)

SESSION_HEADER = "x-lens-session"
PROJECT_HEADER = "x-lens-project"
# Not forwarded: hop-by-hop headers, headers httpx sets itself, and lens's own headers.
DROPPED_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "accept-encoding",
        SESSION_HEADER,
        PROJECT_HEADER,
    }
)
# httpx decodes the body, so its length and encoding no longer apply.
DROPPED_RESPONSE_HEADERS = frozenset(
    {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}
)


def upstream_headers(request: Request) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in DROPPED_REQUEST_HEADERS
    }


def client_headers(upstream: httpx.Response) -> list[tuple[str, str]]:
    # A list, not a dict: repeated headers (set-cookie) must survive.
    return [
        (name, value)
        for name, value in upstream.headers.multi_items()
        if name.lower() not in DROPPED_RESPONSE_HEADERS
    ]


def error_response(status_code: int, error_type: str, message: str) -> JSONResponse:
    """An error body in the Anthropic API format."""
    return JSONResponse(
        {"type": "error", "error": {"type": error_type, "message": message}},
        status_code=status_code,
    )


def stream_error_event(message: str) -> bytes:
    data = {"type": "error", "error": {"type": "api_error", "message": message}}
    return f"event: error\ndata: {json.dumps(data)}\n\n".encode()


def create_app(
    settings: Settings | None = None,
    *,
    upstream: httpx.AsyncClient | None = None,
    recorder: Recorder | None = None,
    clock: Clock = time.perf_counter,
) -> FastAPI:
    settings = settings or get_settings()
    owns_client = upstream is None
    client = upstream or httpx.AsyncClient(
        base_url=settings.upstream_url, timeout=settings.upstream_timeout
    )
    owns_recorder = recorder is None
    rec = recorder or Recorder(lambda: Store(settings.resolved_db_path()))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_client:
            await client.aclose()
        if owns_recorder:
            rec.close()

    app = FastAPI(title="claude-lens", lifespan=lifespan)
    app.state.recorder = rec

    @app.get("/healthz")
    async def healthz() -> dict[str, str | int]:
        return {
            "status": "ok",
            "upstream": settings.upstream_url,
            "recorded": rec.recorded,
            "record_errors": rec.errors,
        }

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def forward(path: str, request: Request) -> Response:
        started = clock()
        started_at = datetime.now(UTC)
        body = await request.body()
        upstream_request = client.build_request(
            request.method,
            httpx.URL(path=f"/v1/{path}", query=request.url.query.encode()),
            content=body,
            headers=upstream_headers(request),
        )
        try:
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            log.error("upstream_request_failed", error_type=type(exc).__name__)
            return error_response(502, "api_error", "claude-lens could not reach the upstream API")

        job: RecordJob | None = None
        content_type = upstream_response.headers.get("content-type", "")
        if request.method == "POST" and path == "messages" and upstream_response.status_code == 200:
            tee = ResponseTee(
                sse=content_type.startswith("text/event-stream"), started=started, clock=clock
            )
            job = RecordJob(
                started_at=started_at,
                request_body=body,
                request_id=upstream_response.headers.get("request-id"),
                session_header=request.headers.get(SESSION_HEADER),
                project_header=request.headers.get(PROJECT_HEADER),
                tee=tee,
            )
        response = StreamingResponse(
            relay(upstream_response, job, sse=content_type.startswith("text/event-stream")),
            status_code=upstream_response.status_code,
        )
        response.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in client_headers(upstream_response)
        ]
        return response

    async def relay(
        upstream_response: httpx.Response, job: RecordJob | None, *, sse: bool
    ) -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_response.aiter_bytes():
                yield chunk
                if job is not None:
                    job.tee.feed(chunk)
        except httpx.HTTPError as exc:
            log.error("upstream_stream_failed", error_type=type(exc).__name__)
            if sse:
                yield stream_error_event("claude-lens lost the upstream stream")
        finally:
            # Also reached when the client disconnects: a partial response was still billed.
            if job is not None:
                job.tee.close()
                rec.submit(job)
            with suppress(Exception):
                await upstream_response.aclose()

    return app
