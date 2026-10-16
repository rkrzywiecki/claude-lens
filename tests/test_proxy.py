"""Proxy end to end: respx plays api.anthropic.com, the app runs in-process over ASGI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from claude_lens.cli import app as cli_app
from claude_lens.config import Settings
from claude_lens.models import RequestRecord
from claude_lens.proxy.app import create_app
from claude_lens.proxy.recorder import Recorder
from claude_lens.store import Store

UPSTREAM = "https://upstream.test"
HEADERS = {
    "x-api-key": "sk-ant-api03-test",
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}


def sse(*events: dict[str, Any]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def stream_events(message_id: str = "msg_01", *, tool: str | None = "Bash") -> list[bytes]:
    """Chunks of a streamed response, shaped like the real API's events."""
    start = {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [],
            "stop_reason": None,
            "usage": {
                "input_tokens": 12,
                "cache_creation_input_tokens": 300,
                "cache_read_input_tokens": 5000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 300,
                },
                "output_tokens": 1,
            },
        },
    }
    chunks = [sse(start, {"type": "ping"})]
    chunks.append(
        sse(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hi"},
            },
            {"type": "content_block_stop", "index": 0},
        )
    )
    if tool:
        chunks.append(
            sse(
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": tool,
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"command": "ls"}'},
                },
                {"type": "content_block_stop", "index": 1},
            )
        )
    end = sse(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 42},
        },
        {"type": "message_stop"},
    )
    # Split an event across two chunks, as TCP does.
    chunks.extend([end[:17], end[17:]])
    return chunks


def json_message(message_id: str = "msg_02") -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 7,
            "output_tokens": 9,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 100,
            "speed": "fast",
        },
    }


async def body_stream(chunks: list[bytes], delay: float = 0.0) -> AsyncIterator[bytes]:
    for chunk in chunks:
        if delay:
            await asyncio.sleep(delay)
        yield chunk


def request_body(messages: list[dict[str, Any]], *, stream: bool = True, **extra: Any) -> bytes:
    body = {"model": "claude-opus-5-5", "max_tokens": 1024, "messages": messages, **extra}
    if stream:
        body["stream"] = True
    return json.dumps(body).encode()


USER = {"role": "user", "content": "hello"}


class Harness:
    def __init__(self, recorder: Recorder, db: Path, client: httpx.AsyncClient) -> None:
        self.recorder = recorder
        self.db = db
        self.client = client

    def records(self) -> list[RequestRecord]:
        self.recorder.drain()
        with Store(self.db) as store:
            return list(store.requests())


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=UPSTREAM, assert_all_called=False) as router:
        yield router


def make_harness(tmp_path: Path, open_store: Callable[[], Store] | None = None) -> Harness:
    db = tmp_path / "proxy.db"
    recorder = Recorder(open_store or (lambda: Store(db)))
    app = create_app(
        Settings(upstream_url=UPSTREAM),
        upstream=httpx.AsyncClient(base_url=UPSTREAM),
        recorder=recorder,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lens")
    return Harness(recorder, db, client)


@pytest.fixture
def lens(tmp_path: Path) -> Iterator[Harness]:
    harness = make_harness(tmp_path)
    yield harness
    harness.recorder.close()


async def test_streaming_is_relayed_unchanged_and_recorded(
    lens: Harness, mock: respx.MockRouter
) -> None:
    chunks = stream_events()
    mock.post("/v1/messages").respond(
        200,
        headers={"content-type": "text/event-stream", "request-id": "req_011"},
        content=body_stream(chunks),
    )
    response = await lens.client.post("/v1/messages", content=request_body([USER]), headers=HEADERS)
    assert response.status_code == 200
    assert response.content == b"".join(chunks)
    assert response.headers["content-type"] == "text/event-stream"
    assert response.headers["request-id"] == "req_011"

    [record] = lens.records()
    assert record.message_id == "msg_01"
    assert record.request_id == "req_011"
    assert record.model == "claude-opus-5-5"
    assert record.source == "proxy"
    assert record.stop_reason == "tool_use"
    assert record.tool_names == ["Bash"]
    assert record.usage.input_tokens == 12
    assert record.usage.output_tokens == 42  # final count from message_delta
    assert record.usage.cache_read_tokens == 5000
    assert record.usage.cache_write_1h_tokens == 300
    assert record.usage.cache_write_5m_tokens == 0
    assert record.ttft_ms is not None
    assert record.latency_ms is not None
    assert record.latency_ms >= record.ttft_ms


async def test_message_delta_usage_overrides_start(lens: Harness, mock: respx.MockRouter) -> None:
    chunks = stream_events(tool=None)
    chunks[-2:] = [
        sse(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5, "input_tokens": 20, "cache_read_input_tokens": None},
            }
        )
    ]
    mock.post("/v1/messages").respond(
        200, headers={"content-type": "text/event-stream"}, content=body_stream(chunks)
    )
    await lens.client.post("/v1/messages", content=request_body([USER]), headers=HEADERS)
    [record] = lens.records()
    assert record.usage.input_tokens == 20
    assert record.usage.output_tokens == 5
    assert record.usage.cache_read_tokens == 5000  # null in the delta keeps the start value
    assert record.stop_reason == "end_turn"
    assert record.tool_names == []


async def test_latency_fields_measure_time(lens: Harness, mock: respx.MockRouter) -> None:
    mock.post("/v1/messages").respond(
        200,
        headers={"content-type": "text/event-stream"},
        content=body_stream(stream_events(), delay=0.03),
    )
    await lens.client.post("/v1/messages", content=request_body([USER]), headers=HEADERS)
    [record] = lens.records()
    assert record.ttft_ms is not None and record.latency_ms is not None
    # The first content block arrives in the second chunk, the stream ends after five.
    assert record.ttft_ms >= 50
    assert record.latency_ms >= 140
    assert record.latency_ms > record.ttft_ms


async def test_non_streaming_response_is_recorded(lens: Harness, mock: respx.MockRouter) -> None:
    raw = json.dumps(json_message()).encode()
    mock.post("/v1/messages").respond(
        200, headers={"content-type": "application/json", "request-id": "req_2"}, content=raw
    )
    response = await lens.client.post(
        "/v1/messages", content=request_body([USER], stream=False), headers=HEADERS
    )
    assert response.status_code == 200
    assert response.content == raw

    [record] = lens.records()
    assert record.message_id == "msg_02"
    assert record.model == "claude-sonnet-5-5"
    assert record.usage.output_tokens == 9
    assert record.usage.cache_read_tokens == 100
    assert record.speed == "fast"
    assert record.tool_names == ["Read"]
    assert record.ttft_ms is None
    assert record.latency_ms is not None


@pytest.mark.parametrize("stream", [True, False])
async def test_upstream_errors_pass_through_verbatim(
    lens: Harness, mock: respx.MockRouter, stream: bool
) -> None:
    error = b'{"type":"error","error":{"type":"rate_limit_error","message":"Slow down"}}'
    mock.post("/v1/messages").respond(
        429,
        headers={"content-type": "application/json", "retry-after": "17", "request-id": "req_e"},
        content=error,
    )
    response = await lens.client.post(
        "/v1/messages", content=request_body([USER], stream=stream), headers=HEADERS
    )
    assert response.status_code == 429
    assert response.content == error
    assert response.headers["retry-after"] == "17"
    assert response.headers["request-id"] == "req_e"
    assert lens.records() == []


async def test_unreachable_upstream_returns_api_error(
    lens: Harness, mock: respx.MockRouter
) -> None:
    mock.post("/v1/messages").mock(side_effect=httpx.ConnectError("refused"))
    response = await lens.client.post("/v1/messages", content=request_body([USER]), headers=HEADERS)
    assert response.status_code == 502
    assert response.json()["type"] == "error"
    assert response.json()["error"]["type"] == "api_error"


async def test_broken_upstream_stream_ends_with_error_event_and_records_partial(
    lens: Harness, mock: respx.MockRouter
) -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield stream_events()[0]
        raise httpx.ReadError("connection reset")

    mock.post("/v1/messages").respond(
        200, headers={"content-type": "text/event-stream"}, content=broken()
    )
    response = await lens.client.post("/v1/messages", content=request_body([USER]), headers=HEADERS)
    assert response.content.startswith(stream_events()[0])
    assert response.content.endswith(b"\n\n")
    assert b"event: error" in response.content
    [record] = lens.records()
    assert record.usage.cache_read_tokens == 5000  # billed even though the stream broke


async def test_headers_are_forwarded_and_lens_headers_stripped(
    lens: Harness, mock: respx.MockRouter
) -> None:
    route = mock.post("/v1/messages").respond(
        200, headers={"content-type": "application/json"}, json=json_message()
    )
    await lens.client.post(
        "/v1/messages?beta=true",
        content=request_body([USER], stream=False),
        headers={
            **HEADERS,
            "authorization": "Bearer sk-ant-oat01-test",
            "anthropic-beta": "prompt-caching-2024-07-31",
            "user-agent": "claude-cli/2.1.0",
            "x-lens-session": "my-session",
            "x-lens-project": "/work/proj",
        },
    )
    sent = route.calls.last.request
    assert sent.url == f"{UPSTREAM}/v1/messages?beta=true"
    assert sent.headers["x-api-key"] == "sk-ant-api03-test"
    assert sent.headers["authorization"] == "Bearer sk-ant-oat01-test"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert sent.headers["anthropic-beta"] == "prompt-caching-2024-07-31"
    assert sent.headers["user-agent"] == "claude-cli/2.1.0"
    assert sent.headers["host"] == "upstream.test"
    assert "x-lens-session" not in sent.headers
    assert "x-lens-project" not in sent.headers
    assert sent.content == request_body([USER], stream=False)

    [record] = lens.records()
    assert record.session_id == "my-session"
    assert record.project == "/work/proj"


async def test_credentials_are_not_logged(
    lens: Harness, mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    mock.post("/v1/messages").mock(side_effect=httpx.ConnectError("refused"))
    await lens.client.post("/v1/messages", content=request_body([USER]), headers=HEADERS)
    captured = capsys.readouterr()
    assert "sk-ant-api03-test" not in captured.err + captured.out


async def test_other_endpoints_pass_through_without_records(
    lens: Harness, mock: respx.MockRouter
) -> None:
    mock.post("/v1/messages/count_tokens").respond(200, json={"input_tokens": 12})
    mock.get("/v1/models").respond(200, json={"data": [{"id": "claude-opus-5-5"}]})
    counted = await lens.client.post(
        "/v1/messages/count_tokens", content=request_body([USER], stream=False), headers=HEADERS
    )
    models = await lens.client.get("/v1/models?limit=5", headers=HEADERS)
    assert counted.json() == {"input_tokens": 12}
    assert models.json()["data"][0]["id"] == "claude-opus-5-5"
    assert lens.records() == []


async def test_healthz(lens: Harness) -> None:
    response = await lens.client.get("/healthz")
    assert response.json()["status"] == "ok"
    assert response.json()["upstream"] == UPSTREAM


async def test_broken_store_fails_open(tmp_path: Path, mock: respx.MockRouter) -> None:
    def broken_store() -> Store:
        raise OSError("disk full")

    harness = make_harness(tmp_path, broken_store)
    chunks = stream_events()
    mock.post("/v1/messages").respond(
        200, headers={"content-type": "text/event-stream"}, content=body_stream(chunks)
    )
    try:
        response = await harness.client.post(
            "/v1/messages", content=request_body([USER]), headers=HEADERS
        )
        assert response.status_code == 200
        assert response.content == b"".join(chunks)
        harness.recorder.drain()
        assert harness.recorder.errors == 1
        assert harness.recorder.recorded == 0
    finally:
        harness.recorder.close()


async def test_garbage_stream_fails_open(lens: Harness, mock: respx.MockRouter) -> None:
    garbage = b"data: {not json\n\nevent: x\ndata: [1, 2]\n\n\xff\xfe\n\n"
    mock.post("/v1/messages").respond(
        200, headers={"content-type": "text/event-stream"}, content=garbage
    )
    response = await lens.client.post("/v1/messages", content=request_body([USER]), headers=HEADERS)
    assert response.content == garbage
    assert lens.records() == []
    assert lens.recorder.skipped == 1


async def test_conversation_turns_share_a_session(lens: Harness, mock: respx.MockRouter) -> None:
    replies = iter(["msg_a", "msg_b", "msg_c"])

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, json=json_message(next(replies))
        )

    mock.post("/v1/messages").mock(side_effect=respond)
    system = [{"type": "text", "text": "You are Claude Code.\nPrimary working directory: /w/app"}]
    turn1 = [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {}}]}]
    turn2 = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": [{"type": "text", "text": "more", "cache_control": {}}]},
    ]
    other = [{"role": "user", "content": "unrelated"}]
    for messages in (turn1, turn2, other):
        await lens.client.post(
            "/v1/messages",
            content=request_body(messages, stream=False, system=system),
            headers=HEADERS,
        )
    by_id = {r.message_id: r for r in lens.records()}
    assert by_id["msg_a"].session_id == by_id["msg_b"].session_id
    assert by_id["msg_c"].session_id != by_id["msg_a"].session_id
    assert by_id["msg_a"].project == "/w/app"
    assert by_id["msg_a"].session_id.startswith("proxy-")


def test_serve_refuses_non_loopback_bind() -> None:
    result = CliRunner().invoke(cli_app, ["serve", "--host", "0.0.0.0"])  # noqa: S104
    assert result.exit_code == 2
    assert "non-loopback" in result.output
