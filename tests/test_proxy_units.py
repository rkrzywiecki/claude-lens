import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_lens.config import is_loopback
from claude_lens.models import RequestRecord, Usage
from claude_lens.proxy.sessions import (
    DEFAULT_PROJECT,
    SessionTracker,
    client_session_id,
    prefix_hashes,
    working_directory,
)
from claude_lens.proxy.tee import ResponseTee, SSEParser
from claude_lens.store import SCHEMA_VERSION, Store

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def test_sse_parser_handles_split_chunks_and_crlf() -> None:
    parser = SSEParser()
    assert parser.feed(b'event: a\r\ndata: {"x"') == []
    assert parser.feed(b": 1}\r\n\r\nevent: b\ndata: 2\n") == ['{"x": 1}']
    assert parser.flush() == ["2"]


def test_tee_ttft_uses_first_content_block() -> None:
    # Read once at the first content block and once when the stream closes.
    ticks = iter([1.25, 1.5])
    tee = ResponseTee(sse=True, started=1.0, clock=lambda: next(ticks))
    tee.feed(b'data: {"type":"message_start","message":{"id":"m","model":"x","usage":{}}}\n\n')
    tee.feed(b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n')
    tee.close()
    observation = tee.observation()
    assert observation is not None
    assert observation.ttft_ms == 250.0
    assert observation.latency_ms == 500.0


def test_tee_without_message_id_observes_nothing() -> None:
    tee = ResponseTee(sse=False, started=0.0, clock=lambda: 0.0)
    tee.feed(b'{"type": "error"}')
    tee.close()
    assert tee.observation() is None


def test_prefix_hashes_ignore_cache_control_and_string_content() -> None:
    a = {"messages": [{"role": "user", "content": "hi"}]}
    b = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {}}]},
            {"role": "assistant", "content": "ok"},
        ]
    }
    assert prefix_hashes(a)[0] == prefix_hashes(b)[0]
    assert prefix_hashes({**a, "system": "other"})[0] != prefix_hashes(a)[0]
    assert prefix_hashes({"messages": "nope"}) == []


def test_tracker_follows_forks_and_evicts() -> None:
    tracker = SessionTracker(capacity=2)
    m1 = {"role": "user", "content": "one"}
    m2 = {"role": "assistant", "content": "two"}
    first = tracker.assign({"messages": [m1]})
    # A rewind: the history diverges after m1, still the same conversation.
    fork = tracker.assign({"messages": [m1, {"role": "assistant", "content": "x"}]})
    assert fork.session_id == first.session_id
    tracker.assign({"messages": [{"role": "user", "content": "a"}]})
    tracker.assign({"messages": [{"role": "user", "content": "b"}]})
    assert len(tracker) == 2
    # Both prefixes of the first conversation were evicted.
    assert tracker.assign({"messages": [m1, m2]}).session_id != first.session_id


def test_tracker_without_messages_still_gets_a_session() -> None:
    info = SessionTracker().assign(None)
    assert info.project == DEFAULT_PROJECT
    assert info.session_id.startswith("proxy-")


def test_working_directory_from_system_prompt_or_first_message() -> None:
    assert working_directory({"system": "x\n - Primary working directory: /a/b c\n"}) == "/a/b c"
    assert working_directory({"system": [{"type": "text", "text": "Working directory: /x"}]}) == (
        "/x"
    )
    env = {"role": "system", "content": [{"type": "text", "text": "Working directory: /m"}]}
    prompt = {"role": "user", "content": "hi"}
    reply = {"role": "assistant", "content": "ok"}
    assert working_directory({"system": "none", "messages": [prompt, env]}) == "/m"
    assert working_directory({"messages": [prompt, reply, env]}) is None
    assert working_directory({"system": "Working directory: relative"}) is None
    assert working_directory({}) is None


def test_client_session_id_from_metadata() -> None:
    sid = "b9882c34-7748-492f-94d9-76bc71de04a0"
    as_json = json.dumps({"device_id": "d", "account_uuid": "a", "session_id": sid})
    assert client_session_id({"metadata": {"user_id": as_json}}) == sid
    legacy = f"user_abc_account_7bb1729f-f9f7-4110-85c5-8a5cb067eee2_session_{sid}"
    assert client_session_id({"metadata": {"user_id": legacy}}) == sid
    assert client_session_id({"metadata": {"user_id": "someone"}}) is None
    assert client_session_id({"metadata": {"user_id": "[1]"}}) is None
    assert client_session_id({}) is None


def test_metadata_session_wins_over_prefix() -> None:
    tracker = SessionTracker()
    messages = [{"role": "user", "content": "hi"}]
    first = tracker.assign({"messages": messages})
    meta = {"user_id": json.dumps({"session_id": "cc-1"})}
    assert tracker.assign({"messages": messages, "metadata": meta}).session_id == "cc-1"
    assert first.session_id != "cc-1"


def test_side_call_inherits_the_session_project() -> None:
    tracker = SessionTracker()
    meta = {"user_id": json.dumps({"session_id": "cc-2"})}
    env = {"role": "user", "content": "Primary working directory: /w/app"}
    assert tracker.assign({"messages": [env], "metadata": meta}).project == "/w/app"
    title = {"messages": [{"role": "user", "content": "name this"}], "metadata": meta}
    assert tracker.assign(title).project == "/w/app"


@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1", True), ("localhost", True), ("::1", True), ("0.0.0.0", False), ("a.b", False)],  # noqa: S104
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert is_loopback(host) is expected


V1_SCHEMA = """
CREATE TABLE requests (
    message_id TEXT PRIMARY KEY, request_id TEXT, session_id TEXT NOT NULL,
    project TEXT NOT NULL, model TEXT NOT NULL, ts TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cache_read INTEGER NOT NULL,
    cache_write_5m INTEGER NOT NULL, cache_write_1h INTEGER NOT NULL, speed TEXT NOT NULL,
    stop_reason TEXT, tool_names TEXT NOT NULL, is_sidechain INTEGER NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE imported_files (path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL);
INSERT INTO requests VALUES ('m1', NULL, 's', '/p', 'claude-opus-5-5',
    '2026-10-12T12:00:00.000000+00:00', 1, 2, 3, 4, 5, 'standard', 'end_turn', '[]', 0,
    'claude_code');
PRAGMA user_version = 1;
"""


def test_migrates_v1_database(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.close()
    with Store(path) as store:
        [record] = store.requests()
        assert record.usage.cache_write_1h_tokens == 5
        assert record.ttft_ms is None
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    conn.close()
    Store(path).close()  # reopening a current database is a no-op


def _record(source: str, session: str, latency: float | None, output: int) -> RequestRecord:
    return RequestRecord(
        message_id="msg_1",
        session_id=session,
        project=f"/{source}",
        model="claude-opus-5-5",
        timestamp=T0,
        usage=Usage(output_tokens=output),
        source=source,  # type: ignore[arg-type]
        latency_ms=latency,
        ttft_ms=latency,
    )


def test_proxy_record_keeps_imported_identity(store: Store) -> None:
    store.upsert([_record("claude_code", "cc-session", None, 10)])
    store.upsert([_record("proxy", "proxy-x", 120.0, 12)])
    [merged] = store.requests()
    assert (merged.session_id, merged.project, merged.source) == (
        "cc-session",
        "/claude_code",
        "claude_code",
    )
    assert merged.latency_ms == 120.0
    assert merged.usage.output_tokens == 12


def test_import_keeps_proxy_latency(store: Store) -> None:
    store.upsert([_record("proxy", "proxy-x", 120.0, 12)])
    store.upsert([_record("claude_code", "cc-session", None, 12)])
    [merged] = store.requests()
    assert merged.session_id == "cc-session"
    assert merged.source == "claude_code"
    assert merged.ttft_ms == merged.latency_ms == 120.0
