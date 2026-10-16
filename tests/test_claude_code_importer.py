import os
from pathlib import Path

from conftest import assistant, tool_use, write_jsonl

from claude_lens.importers.claude_code import import_transcripts, parse_transcript, parse_usage
from claude_lens.models import ImportStats
from claude_lens.store import Store


def test_parse_usage_splits_cache_writes_by_ttl() -> None:
    usage = parse_usage(
        {
            "input_tokens": 2,
            "output_tokens": 143,
            "cache_read_input_tokens": 38885,
            "cache_creation_input_tokens": 20707,
            "cache_creation": {
                "ephemeral_1h_input_tokens": 20000,
                "ephemeral_5m_input_tokens": 707,
            },
        }
    )
    assert usage.cache_write_1h_tokens == 20000
    assert usage.cache_write_5m_tokens == 707
    assert usage.cache_read_tokens == 38885


def test_parse_usage_without_split_counts_writes_as_5m() -> None:
    usage = parse_usage({"cache_creation_input_tokens": 500})
    assert (usage.cache_write_5m_tokens, usage.cache_write_1h_tokens) == (500, 0)


def test_parse_usage_inconsistent_split_keeps_total() -> None:
    usage = parse_usage(
        {"cache_creation_input_tokens": 100, "cache_creation": {"ephemeral_1h_input_tokens": 30}}
    )
    assert usage.cache_write_tokens == 100
    assert usage.cache_write_1h_tokens == 30


def test_parse_usage_ignores_garbage_values() -> None:
    usage = parse_usage(
        {"input_tokens": "12", "output_tokens": -5, "cache_read_input_tokens": True}
    )
    assert usage.prompt_tokens == 0
    assert usage.output_tokens == 0


def test_blocks_of_one_message_merge_into_one_record(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "s.jsonl",
        [
            assistant("msg_1", content=[tool_use("Bash")], output_tokens=40),
            assistant("msg_1", content=[tool_use("Read"), tool_use("Bash")], output_tokens=143),
            assistant("msg_1", content=[{"type": "text", "text": "done"}], output_tokens=90),
        ],
    )
    [record] = parse_transcript(path)
    assert record.usage.output_tokens == 143
    assert record.tool_names == ["Bash", "Read"]


def test_non_api_and_malformed_lines_are_skipped(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "s.jsonl",
        [
            {"type": "user", "sessionId": "sess-1", "message": {"role": "user", "content": "hi"}},
            {"type": "cost-state", "sessionId": "sess-1", "totalCostUSD": 1.0},
            assistant("msg_synthetic", model="<synthetic>"),
            {"type": "assistant", "message": "not a dict"},
            assistant("msg_no_ts", ts="yesterday"),
            "{not json",
            assistant("msg_ok"),
        ],
    )
    stats = ImportStats()
    records = parse_transcript(path, stats)
    assert [r.message_id for r in records] == ["msg_ok"]
    assert stats.bad_lines == 1


def test_record_fields(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "s.jsonl",
        [assistant("msg_1", sidechain=True, speed="fast", model="claude-opus-5-5")],
    )
    [record] = parse_transcript(path)
    assert record.session_id == "sess-1"
    assert record.project == "/work/app"
    assert record.request_id == "req_msg_1"
    assert record.speed == "fast"
    assert record.is_sidechain
    assert record.stop_reason == "end_turn"
    assert record.timestamp.utcoffset() is not None


def test_project_falls_back_to_transcript_directory(tmp_path: Path, store: Store) -> None:
    root = tmp_path / "projects"
    write_jsonl(root / "-work-app" / "s1" / "subagents" / "a.jsonl", [assistant("m", cwd=None)])
    import_transcripts(store, root)
    [record] = store.requests()
    assert record.project == "-work-app"


def test_import_is_incremental_and_idempotent(tmp_path: Path, store: Store) -> None:
    root = tmp_path / "projects"
    path = write_jsonl(root / "-work-app" / "s1.jsonl", [assistant("m1"), assistant("m2")])

    first = import_transcripts(store, root)
    assert (first.files_imported, first.records) == (1, 2)

    second = import_transcripts(store, root)
    assert (second.files_imported, second.files_skipped) == (0, 1)

    write_jsonl(path, [assistant("m1"), assistant("m2"), assistant("m3")])
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))
    third = import_transcripts(store, root)
    assert third.files_imported == 1
    assert store.count() == 3

    full = import_transcripts(store, root, full=True)
    assert full.files_imported == 1
    assert store.count() == 3


def test_same_message_in_two_files_is_counted_once(tmp_path: Path, store: Store) -> None:
    root = tmp_path / "projects"
    write_jsonl(root / "p" / "a.jsonl", [assistant("shared")])
    write_jsonl(root / "p" / "b.jsonl", [assistant("shared")])
    import_transcripts(store, root)
    assert store.count() == 1
