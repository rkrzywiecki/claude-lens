"""Regenerate the synthetic transcripts in examples/trends (the README's `trends` example).

Four weeks of one developer's sessions, getting better at the habits `insights` checks: early
weeks have long sessions that never reset, multi-file changes without plan mode and files
edited over and over, all on Opus at high effort; later weeks clear the context, plan first and
move routine work to Sonnet at medium effort. No real prompt content: prompts are placeholders.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent / "examples" / "trends"
CWD = "/home/dev/shop"
# Mondays of ISO weeks 2026-W39 ... 2026-W42.
MONDAYS = [date(2026, 9, 21) + timedelta(weeks=n) for n in range(4)]


def ts(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(session: str, at: datetime, n: int, mode: str) -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": session,
        "cwd": CWD,
        "timestamp": ts(at),
        "entrypoint": "cli",
        "version": "2.1.296",
        "permissionMode": mode,
        "message": {"role": "user", "content": f"prompt {n}"},
    }


def assistant(
    session: str,
    at: datetime,
    n: int,
    model: str,
    effort: str,
    context: int,
    tool: dict[str, Any],
) -> dict[str, Any]:
    output = 1_200
    return {
        "type": "assistant",
        "sessionId": session,
        "cwd": CWD,
        "timestamp": ts(at),
        "isSidechain": False,
        "effort": effort,
        "message": {
            "id": f"msg_{session[:6]}_{n:03d}",
            "model": model,
            "role": "assistant",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": f"toolu_{session[:6]}_{n:03d}", **tool}],
            "usage": {
                "input_tokens": 3,
                "output_tokens": output,
                "output_tokens_details": {"thinking_tokens": output // 3},
                "cache_read_input_tokens": context - 2_000,
                "cache_creation_input_tokens": 2_000,
            },
        },
    }


def long_session(week: int) -> list[dict[str, Any]]:
    """Ten prompts on one task; from week 3 on the context is cleared halfway."""
    session = f"w{week}long-{MONDAYS[week]:%m%d}"
    start = datetime.combine(MONDAYS[week], datetime.min.time(), UTC) + timedelta(hours=9)
    model, effort = ("claude-opus-5-5", "high") if week < 2 else ("claude-sonnet-5-5", "medium")
    entries: list[dict[str, Any]] = []
    context = 40_000
    for n in range(10):
        at = start + timedelta(minutes=n * 3)
        entries.append(user(session, at, n, "default"))
        if week >= 2 and n == 5:
            context = 40_000  # /clear between tasks
        tool = {"name": "Read", "input": {"file_path": f"{CWD}/src/mod{n}.py"}}
        entries.append(
            assistant(session, at + timedelta(seconds=40), n, model, effort, context, tool)
        )
        context += 18_000
    return entries


def change_session(week: int) -> list[dict[str, Any]]:
    """A change across six files; early weeks skip plan mode and keep re-editing one file."""
    session = f"w{week}change-{MONDAYS[week]:%m%d}"
    start = datetime.combine(MONDAYS[week], datetime.min.time(), UTC) + timedelta(days=2, hours=13)
    planned = week >= 1
    model, effort = ("claude-opus-5-5", "high") if week < 3 else ("claude-sonnet-5-5", "high")
    entries: list[dict[str, Any]] = [user(session, start, 0, "plan" if planned else "default")]
    files = [f"{CWD}/src/file{i}.py" for i in range(6)]
    if week < 2:
        files += [f"{CWD}/src/file0.py"] * 5  # the same file over and over: file churn
    for n, path in enumerate(files):
        at = start + timedelta(minutes=1 + n * 2)
        tool = {"name": "Edit", "input": {"file_path": path, "old_string": str(n)}}
        entries.append(assistant(session, at, n, model, effort, 30_000 + n * 2_000, tool))
    return entries


def main() -> None:
    for week in range(len(MONDAYS)):
        for entries in (long_session(week), change_session(week)):
            path = ROOT / "-home-dev-shop" / f"{entries[0]['sessionId']}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    print(f"wrote {ROOT}")


if __name__ == "__main__":
    main()
