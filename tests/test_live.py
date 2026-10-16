"""Live commands: incremental session reading, status line, nudge hook and the fast entry point."""

from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import assistant, write_jsonl

from claude_lens import entry
from claude_lens.importers.records import parse_usage
from claude_lens.insights.thresholds import KITCHEN_SINK_PEAK_TOKENS, KITCHEN_SINK_PROMPTS
from claude_lens.live import nudges, statusline
from claude_lens.live.session import (
    OPEN_MESSAGES,
    STATE_MAX_AGE_SECONDS,
    LiveSession,
    prune,
)
from claude_lens.models import Usage
from claude_lens.pricing import load_pricing

T0 = "2026-10-16T12:00:00Z"
NOW = datetime(2026, 10, 16, 12, 2, 0, tzinfo=UTC).timestamp()  # two minutes after T0


def prompt(text: str = "do it", session: str = "sess-1") -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": session,
        "timestamp": T0,
        "message": {"role": "user", "content": text},
    }


def tool_result(session: str = "sess-1") -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": session,
        "timestamp": T0,
        "toolUseResult": {"stdout": ""},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "x"}],
        },
    }


def big(message_id: str, context: int, ts: str = T0, **usage: Any) -> dict[str, Any]:
    """A response whose prompt was `context` tokens, all read from cache."""
    return assistant(message_id, ts=ts, input_tokens=0, cache_read_input_tokens=context, **usage)


def usage_of(entry: dict[str, Any]) -> Usage:
    return parse_usage(entry["message"]["usage"])


def transcript(tmp_path: Path, entries: list[dict[str, Any] | str]) -> Path:
    return write_jsonl(tmp_path / "projects" / "-work-app" / "sess-1.jsonl", entries)


def status_json(path: Path, **extra: Any) -> str:
    return json.dumps({"session_id": "sess-1", "transcript_path": str(path), **extra})


def line(path: Path, now: float = NOW, **extra: Any) -> str:
    return statusline.render(status_json(path, **extra), now, color=False)


# -- session reading ---------------------------------------------------------------


def test_snapshot_matches_importer_cost_and_counts_prompts(tmp_path: Path) -> None:
    path = transcript(
        tmp_path,
        [
            prompt(),
            # Two content blocks of one response: one record, not two.
            assistant("m1", ts=T0, output_tokens=10),
            assistant("m1", ts=T0, output_tokens=500),
            tool_result(),
            prompt("next"),
            assistant("m2", ts=T0, model="claude-sonnet-5-5"),
        ],
    )
    snap = LiveSession.open(path).snapshot()
    pricing = load_pricing()
    opus = pricing.cost("claude-opus-5-5", usage_of(assistant("m1", output_tokens=500)))
    sonnet = pricing.cost("claude-sonnet-5-5", usage_of(assistant("m2")))
    assert opus is not None and sonnet is not None
    expected = opus.total + sonnet.total
    assert snap.requests == 2
    assert snap.human_prompts == 2
    assert snap.cost_usd == pytest.approx(expected)
    assert snap.model == "claude-sonnet-5-5"


def test_incremental_reads_resume_and_wait_for_complete_lines(tmp_path: Path) -> None:
    path = transcript(tmp_path, [prompt(), big("m1", 1000)])
    first = LiveSession.open(path)
    assert first.snapshot().requests == 1
    first.save()

    # A half-written line has no newline yet: it must not be counted or skipped.
    half = json.dumps(big("m2", 5000))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(half[:40])
    second = LiveSession.open(path)
    assert second.state.files[str(path)].offset > 0
    assert second.snapshot().requests == 1
    second.save()

    with path.open("a", encoding="utf-8") as fh:
        fh.write(half[40:] + "\n")
    snap = LiveSession.open(path).snapshot()
    assert snap.requests == 2
    assert snap.context_tokens == 5000


def test_many_responses_fold_into_totals(tmp_path: Path) -> None:
    count = OPEN_MESSAGES + 10
    path = transcript(tmp_path, [big(f"m{i}", 1000 + i) for i in range(count)])
    session = LiveSession.open(path)
    snap = session.snapshot()
    one = load_pricing().cost("claude-opus-5-5", usage_of(big("x", 1000)))
    assert one is not None
    assert snap.requests == count
    assert snap.cost_usd == pytest.approx(one.total * count, rel=0.01)
    assert len(session.state.files[str(path)].open) == OPEN_MESSAGES


def test_subagent_transcripts_add_cost_not_context(tmp_path: Path) -> None:
    path = transcript(tmp_path, [big("m1", 10_000)])
    write_jsonl(
        path.with_suffix("") / "subagents" / "agent-a1.jsonl",
        [assistant("s1", ts=T0, sidechain=True, cache_read_input_tokens=900_000)],
    )
    snap = LiveSession.open(path).snapshot()
    assert snap.requests == 2
    assert snap.context_tokens == 10_000


def test_rewritten_transcript_is_read_again(tmp_path: Path) -> None:
    path = transcript(tmp_path, [big("m1", 1000), big("m2", 2000)])
    session = LiveSession.open(path)
    session.snapshot()
    session.save()
    # Rewritten in place: same inode, so only the content tells.
    transcript(tmp_path, [big("m9", 3000), big("m10", 3000), big("m11", 3000)])
    snap = LiveSession.open(path).snapshot()
    assert snap.requests == 3
    assert snap.context_tokens == 3000


def test_prune_removes_only_old_state(tmp_path: Path) -> None:
    old, fresh = tmp_path / "old.json", tmp_path / "fresh.json"
    old.write_text("{}")
    fresh.write_text("{}")
    os.utime(old, (0, 0))
    prune(tmp_path, now=STATE_MAX_AGE_SECONDS + 10)
    assert not old.exists()
    assert fresh.exists()


# -- status line -------------------------------------------------------------------


def test_statusline_shows_context_cost_and_warm_5m_cache(tmp_path: Path) -> None:
    path = transcript(tmp_path, [prompt(), big("m1", 182_000)])
    text = line(path, model={"display_name": "Opus"})
    parts = text.split(" · ")
    assert parts[0] == "Opus"
    assert parts[1] == "ctx 182k/1M (18%)"
    assert parts[2].startswith("$")
    assert parts[3] == "cache warm 3m left (5m)"


def test_statusline_uses_1h_ttl_when_the_session_writes_1h_cache(tmp_path: Path) -> None:
    path = transcript(
        tmp_path,
        [
            assistant(
                "m1",
                ts=T0,
                cache_creation_input_tokens=5000,
                cache_creation={"ephemeral_1h_input_tokens": 5000, "ephemeral_5m_input_tokens": 0},
            )
        ],
    )
    assert line(path).endswith("cache warm 58m left (1h)")
    assert line(path, now=NOW + 3600).endswith("cache cold (1h)")


def test_statusline_cache_goes_cold_after_ttl(tmp_path: Path) -> None:
    path = transcript(tmp_path, [big("m1", 50_000)])
    assert line(path, now=NOW + 240).endswith("cache cold (5m)")


def test_statusline_prefers_claude_codes_prompt_cache_and_window(tmp_path: Path) -> None:
    path = transcript(tmp_path, [big("m1", 50_000)])
    text = line(
        path,
        context_window={"context_window_size": 200_000},
        prompt_cache={"warm": True, "ttl": "1h", "expires_at": NOW + 30 * 60},
    )
    assert "ctx 50k/200k (25%)" in text
    assert text.endswith("cache warm 30m left (1h)")
    # The window Claude Code reported is remembered for the hook.
    assert LiveSession.open(path).state.window_hint == 200_000


def test_statusline_falls_back_to_stdin_numbers_without_transcript(tmp_path: Path) -> None:
    data = {
        "transcript_path": str(tmp_path / "missing.jsonl"),
        "model": {"display_name": "Sonnet"},
        "context_window": {"context_window_size": 200_000, "total_input_tokens": 20_000},
        "cost": {"total_cost_usd": 1.5},
    }
    text = statusline.render(json.dumps(data), NOW, color=False)
    assert text == "Sonnet · ctx 20k/200k (10%) · $1.50"


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"transcript_path": 5}'])
def test_statusline_never_fails(stdin: str) -> None:
    assert statusline.render(stdin, NOW, color=False) == "claude-lens"


def test_statusline_colors_a_large_context(tmp_path: Path) -> None:
    path = transcript(tmp_path, [big("m1", KITCHEN_SINK_PEAK_TOKENS)])
    colored = statusline.render(status_json(path), NOW, color=True)
    assert statusline.YELLOW + "ctx 150k" in colored
    path = transcript(tmp_path, [big("m1", 700_000)])
    assert statusline.RED in statusline.render(status_json(path), NOW, color=True)


def test_statusline_marks_unpriced_cost(tmp_path: Path) -> None:
    path = transcript(tmp_path, [assistant("m1", ts=T0, model="claude-unknown-9")])
    assert "$0.00+" in line(path)


def test_labels() -> None:
    assert statusline.tokens_label(999) == "999"
    assert statusline.tokens_label(1_500_000) == "1.5M"
    assert statusline.duration_label(30) == "<1m"
    assert statusline.duration_label(3 * 3600 + 5 * 60) == "3h05m"


# -- nudge hook --------------------------------------------------------------------


def hook_json(path: Path, session: str = "sess-1") -> str:
    return json.dumps(
        {
            "session_id": session,
            "transcript_path": str(path),
            "cwd": "/work/app",
            "permission_mode": "default",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "next task",
        }
    )


def long_session(tmp_path: Path, prompts: int, context: int) -> Path:
    entries: list[dict[str, Any] | str] = []
    for i in range(prompts):
        entries += [prompt(f"p{i}"), big(f"m{i}", context)]
    return transcript(tmp_path, entries)


def run_hook(path: Path, session: str = "sess-1") -> dict[str, Any] | None:
    out = nudges.respond(hook_json(path, session), "UserPromptSubmit")
    return json.loads(out) if out else None


def test_no_nudge_below_thresholds(tmp_path: Path) -> None:
    path = long_session(tmp_path, KITCHEN_SINK_PROMPTS - 1, KITCHEN_SINK_PEAK_TOKENS + 1)
    assert run_hook(path) is None
    path = long_session(tmp_path, KITCHEN_SINK_PROMPTS, KITCHEN_SINK_PEAK_TOKENS - 10)
    assert run_hook(path) is None


def test_kitchen_sink_nudge_fires_once_per_session(tmp_path: Path) -> None:
    path = long_session(tmp_path, KITCHEN_SINK_PROMPTS, KITCHEN_SINK_PEAK_TOKENS)
    out = run_hook(path)
    assert out is not None
    assert set(out) == {"systemMessage"}  # never blocks, never adds context
    message = out["systemMessage"]
    assert message.startswith("claude-lens: ")
    assert "/rename" in message and "/clear" in message and "/compact" in message
    assert f"after {KITCHEN_SINK_PROMPTS} prompts" in message
    assert run_hook(path) is None
    # Another session id is nudged on its own.
    assert run_hook(path, session="sess-2") is not None


def test_near_limit_nudge_then_kitchen_sink_later(tmp_path: Path) -> None:
    path = long_session(tmp_path, 2, 650_000)
    out = run_hook(path)
    assert out is not None
    assert "65% of the model's window" in out["systemMessage"]
    assert "/clear" not in out["systemMessage"]

    path = long_session(tmp_path, KITCHEN_SINK_PROMPTS, 650_000)
    out = run_hook(path)
    assert out is not None
    assert "window" not in out["systemMessage"]
    assert "/rename" in out["systemMessage"]
    assert run_hook(path) is None


def test_near_limit_uses_the_window_seen_by_the_status_line(tmp_path: Path) -> None:
    path = long_session(tmp_path, 1, 130_000)
    assert run_hook(path) is None  # 13% of the 1M window in pricing.yaml
    statusline.render(
        status_json(path, context_window={"context_window_size": 200_000}), NOW, color=False
    )
    path = long_session(tmp_path, 2, 130_000)
    out = run_hook(path)
    assert out is not None
    assert "65% of the model's window" in out["systemMessage"]


@pytest.mark.parametrize("stdin", ["", "garbage", '{"session_id": "x"}'])
def test_hook_ignores_bad_input(stdin: str) -> None:
    assert nudges.respond(stdin, "UserPromptSubmit") == ""


def test_hook_ignores_other_events(tmp_path: Path) -> None:
    path = long_session(tmp_path, KITCHEN_SINK_PROMPTS, KITCHEN_SINK_PEAK_TOKENS)
    assert nudges.respond(hook_json(path), "Stop") == ""


def test_hook_main_exits_zero_on_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_: Any) -> str:
        raise RuntimeError("broken")

    monkeypatch.setattr(nudges, "respond", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert nudges.main("UserPromptSubmit") == 0
    assert capsys.readouterr().out == ""


# -- entry point -------------------------------------------------------------------


def test_entry_fast_path_runs_statusline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = transcript(tmp_path, [big("m1", 20_000)])
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("sys.argv", ["claude-lens", "statusline"])
    monkeypatch.setattr("sys.stdin", io.StringIO(status_json(path)))
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("opus-5-5 · ctx 20k/1M (2%)")


def test_entry_fast_path_runs_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = long_session(tmp_path, KITCHEN_SINK_PROMPTS, KITCHEN_SINK_PEAK_TOKENS)
    monkeypatch.setattr("sys.argv", ["claude-lens", "hook", "UserPromptSubmit"])
    monkeypatch.setattr("sys.stdin", io.StringIO(hook_json(path)))
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 0
    assert "systemMessage" in json.loads(capsys.readouterr().out)
