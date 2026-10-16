"""Builders for synthetic Claude Code transcripts (shapes taken from real 2.1.x transcripts)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from claude_lens.config import get_settings
from claude_lens.store import Store


def assistant(
    message_id: str,
    *,
    session: str = "sess-1",
    model: str = "claude-opus-5-5",
    ts: str = "2026-10-12T16:48:58.924Z",
    cwd: str | None = "/work/app",
    content: list[dict[str, Any]] | None = None,
    sidechain: bool = False,
    **usage: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "assistant",
        "sessionId": session,
        "timestamp": ts,
        "isSidechain": sidechain,
        "requestId": f"req_{message_id}",
        "message": {
            "id": message_id,
            "model": model,
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": content if content is not None else [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 2,
                "output_tokens": 100,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 0,
                **usage,
            },
        },
    }
    if cwd is not None:
        entry["cwd"] = cwd
    return entry


def tool_use(name: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{name}", "name": name, "input": {}}


def write_jsonl(path: Path, entries: list[dict[str, Any] | str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [e if isinstance(e, str) else json.dumps(e) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(Path(":memory:")) as s:
        yield s


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never touch the real ~/.claude or ~/.claude-lens from tests."""
    monkeypatch.setenv("LENS_DB_PATH", str(tmp_path / "lens.db"))
    monkeypatch.setenv("LENS_TRANSCRIPTS_DIR", str(tmp_path / "projects"))
    monkeypatch.delenv("LENS_PRICING_PATH", raising=False)
    # Never reach a real Claude Code CLI (it would spend the user's plan) from a test.
    monkeypatch.setenv("LENS_COACH_CLAUDE_BIN", "claude-lens-test-no-such-cli")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
