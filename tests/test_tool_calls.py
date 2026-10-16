"""Tool calls: transcript import, store merging, proxy recording, schema migration."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import respx
from conftest import assistant, write_jsonl
from test_proxy import HEADERS, USER, Harness, lens, mock, request_body, sse  # noqa: F401
from typer.testing import CliRunner

from claude_lens.cli import app
from claude_lens.importers.claude_code import import_transcripts, parse_transcript_data
from claude_lens.models import ToolCall
from claude_lens.proxy.recorder import tool_results
from claude_lens.store import SCHEMA_VERSION, Store

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
runner = CliRunner()


def tool_use(tool_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def tool_result_entry(tool_id: str, *, error: bool) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_id, "content": "x"}
    if error:
        block["is_error"] = True
    return {"type": "user", "sessionId": "sess-1", "message": {"role": "user", "content": [block]}}


def test_transcript_tool_calls_and_results(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "s.jsonl",
        [
            assistant("m1", content=[tool_use("t1", "Edit", {"file_path": "/a.py", "x": 1})]),
            assistant("m1", content=[tool_use("t2", "Bash", {"command": "pytest"})]),
            tool_result_entry("t1", error=False),
            tool_result_entry("t2", error=True),
            assistant("m2", content=[tool_use("t3", "Read", {"file_path": "/a.py"})]),
        ],
    )
    data = parse_transcript_data(path)
    calls = {c.tool_use_id: c for c in data.tool_calls}
    assert calls["t1"].target == "/a.py"
    assert calls["t1"].is_error is False
    assert calls["t2"].is_error is True
    assert calls["t2"].target is None
    assert calls["t3"].is_error is None  # no result yet
    assert calls["t1"].message_id == "m1"
    assert "pytest" not in calls["t2"].model_dump_json()  # only a hash of the input


def test_results_in_another_file_are_applied(tmp_path: Path, store: Store) -> None:
    root = tmp_path / "projects"
    write_jsonl(root / "p" / "a.jsonl", [assistant("m1", content=[tool_use("t1", "Bash", {})])])
    write_jsonl(root / "p" / "b.jsonl", [tool_result_entry("t1", error=True)])
    stats = import_transcripts(store, root)
    assert stats.tool_calls == 1
    [stored] = store.tool_calls()
    assert stored.is_error is True


def _call(source: str, session: str, is_error: bool | None) -> ToolCall:
    return ToolCall(
        tool_use_id="t1",
        message_id="m1",
        session_id=session,
        timestamp=T0,
        tool_name="Bash",
        input_hash="h",
        is_error=is_error,
        source=source,  # type: ignore[arg-type]
    )


def test_imported_call_keeps_session_and_known_result(store: Store) -> None:
    store.upsert_tool_calls([_call("claude_code", "real", True)])
    store.upsert_tool_calls([_call("proxy", "guess", None)])
    [stored] = store.tool_calls()
    assert (stored.session_id, stored.source, stored.is_error) == ("real", "claude_code", True)


def test_mark_tool_results_ignores_unknown_ids(store: Store) -> None:
    store.upsert_tool_calls([_call("proxy", "s", None)])
    store.mark_tool_results([("t1", False), ("missing", True)])
    [stored] = store.tool_calls()
    assert stored.is_error is False


def test_tool_results_come_from_newest_message() -> None:
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old"}]},
            {"role": "assistant", "content": "ok"},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "is_error": True},
                    {"type": "tool_result", "tool_use_id": "b"},
                    {"type": "text", "text": "go on"},
                ],
            },
        ]
    }
    assert tool_results(body) == [("a", True), ("b", False)]
    assert tool_results({"messages": []}) == []


V2_SCHEMA = """
CREATE TABLE requests (
    message_id TEXT PRIMARY KEY, request_id TEXT, session_id TEXT NOT NULL,
    project TEXT NOT NULL, model TEXT NOT NULL, ts TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cache_read INTEGER NOT NULL,
    cache_write_5m INTEGER NOT NULL, cache_write_1h INTEGER NOT NULL, speed TEXT NOT NULL,
    stop_reason TEXT, tool_names TEXT NOT NULL, is_sidechain INTEGER NOT NULL,
    source TEXT NOT NULL, ttft_ms REAL, latency_ms REAL
);
CREATE TABLE imported_files (path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL);
INSERT INTO requests VALUES ('m1', NULL, 's', '/p', 'claude-opus-5-5',
    '2026-10-12T12:00:00.000000+00:00', 1, 2, 3, 4, 5, 'standard', 'end_turn', '[]', 0,
    'proxy', 10.0, 20.0);
INSERT INTO imported_files VALUES ('/x.jsonl', 1, 1.0);
PRAGMA user_version = 2;
"""


def test_migrates_v2_database(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.executescript(V2_SCHEMA)
    conn.close()
    with Store(path) as store:
        [record] = store.requests()
        assert record.segments is None
        assert record.latency_ms == 20.0
        assert list(store.tool_calls()) == []
        # Imported before tool calls existed: forget the files so they are read again.
        assert not store.is_unchanged(Path("/x.jsonl"), 1, 1.0)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    conn.close()


async def test_proxy_records_segments_and_tool_calls(
    lens: Harness,  # noqa: F811
    mock: respx.MockRouter,  # noqa: F811
) -> None:
    events = sse(
        {
            "type": "message_start",
            "message": {"id": "msg_t", "model": "claude-opus-5-5", "usage": {"input_tokens": 5}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_e", "name": "Edit", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"file_path": "/w/a.py",'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": ' "old_string": "x"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}},
    )
    mock.post("/v1/messages").respond(
        200, headers={"content-type": "text/event-stream"}, content=events
    )
    body = request_body([USER], system="sys", tools=[{"name": "Edit"}])
    response = await lens.client.post("/v1/messages", content=body, headers=HEADERS)
    assert response.status_code == 200
    [record] = lens.records()
    assert record.segments is not None and len(record.segments) == 4
    with Store(lens.db) as store:
        [stored] = store.tool_calls()
    assert (stored.tool_use_id, stored.tool_name, stored.target) == ("toolu_e", "Edit", "/w/a.py")
    assert stored.source == "proxy"

    # The next request answers the call with an error.
    answer = [
        USER,
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_e", "name": "Edit"}]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_e", "is_error": True}],
        },
    ]
    mock.post("/v1/messages").respond(
        200,
        headers={"content-type": "application/json"},
        json={"id": "msg_u", "type": "message", "model": "claude-opus-5-5", "content": []},
    )
    await lens.client.post(
        "/v1/messages", content=request_body(answer, stream=False), headers=HEADERS
    )
    lens.recorder.drain()
    with Store(lens.db) as store:
        [stored] = store.tool_calls()
    assert stored.is_error is True


def _seed(tmp_path: Path) -> None:
    write_jsonl(
        tmp_path / "projects" / "-work-app" / "abc12345-0000.jsonl",
        [
            assistant(
                "m1",
                session="abc12345-0000",
                ts="2026-10-12T10:00:00Z",
                output_tokens=5,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=60_000,
                content=[tool_use("t1", "Edit", {"file_path": "/a.py"})],
            ),
            assistant(
                "m2",
                session="abc12345-0000",
                ts="2026-10-12T10:10:00Z",
                cache_read_input_tokens=0,
                cache_creation_input_tokens=61_000,
            ),
            assistant("m3", session="abd00000-0000", ts="2026-10-12T11:00:00Z"),
        ],
    )


def test_cli_cache_doctor_loops_and_session(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = runner.invoke(app, ["cache-doctor", "--json"])
    assert result.exit_code == 0, result.output
    doctor = json.loads(result.output)
    assert doctor["sessions"][0]["session_id"] == "abc12345-0000"
    assert doctor["sessions"][0]["counts"]["ttl_expired"] == 1

    result = runner.invoke(app, ["cache-doctor"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "avoidable" in result.output

    result = runner.invoke(app, ["loops", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["findings"] == []
    assert "No loops" in runner.invoke(app, ["loops"]).output

    result = runner.invoke(app, ["session", "abc", "--json"])
    assert result.exit_code == 0, result.output
    detail = json.loads(result.output)
    assert detail["session_id"] == "abc12345-0000"
    assert [e["miss_cause"] for e in detail["timeline"]] == [None, "ttl_expired"]

    result = runner.invoke(app, ["session", "abc"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "TTL expired" in result.output


def test_cli_session_errors(tmp_path: Path) -> None:
    _seed(tmp_path)
    ambiguous = runner.invoke(app, ["session", "ab"])
    assert ambiguous.exit_code == 2
    assert "matches 2 sessions" in ambiguous.output
    assert runner.invoke(app, ["session", "zzz"]).exit_code == 2
