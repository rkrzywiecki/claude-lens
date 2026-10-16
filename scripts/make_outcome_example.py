"""Build the git outcome example in examples/outcomes (git-ignored; paths are machine-specific).

Creates a small git repository with commits at fixed dates and synthetic Claude Code
transcripts whose sessions ran in it, for the README.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "examples" / "outcomes"
REPO = ROOT / "repo"
TRANSCRIPTS = ROOT / "transcripts"
EMAIL = "dev@example.com"

# (session, cwd, model, effort, request minutes after 09:00 UTC, edits, output tokens)
SESSIONS = [
    ("3a1f-opus-feature", REPO, "claude-opus-5-5", "high", range(0, 40, 4), True, 4_000),
    ("5c2d-sonnet-fix", REPO, "claude-sonnet-5-5", "medium", range(60, 80, 4), True, 2_000),
    ("8e4b-opus-abandoned", REPO, "claude-opus-5-5", "high", range(120, 170, 5), True, 20_000),
]
# (minute after 09:00 UTC, file, lines): commits made during the first two sessions.
COMMITS = [(10, "app.py", 80), (35, "tests.py", 40), (75, "app.py", 25)]


def git(*args: str, minute: int | None = None) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    env |= {"GIT_AUTHOR_NAME": "Dev", "GIT_COMMITTER_NAME": "Dev"}
    env |= {"GIT_AUTHOR_EMAIL": EMAIL, "GIT_COMMITTER_EMAIL": EMAIL}
    if minute is not None:
        stamp = f"2026-10-14T{9 + minute // 60:02d}:{minute % 60:02d}:00+0000"
        env |= {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(["git", "-C", str(REPO), *args], env=env, check=True, capture_output=True)  # noqa: S603, S607


def main() -> None:
    shutil.rmtree(ROOT, ignore_errors=True)
    REPO.mkdir(parents=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", EMAIL)
    for minute, name, lines in COMMITS:
        path = REPO / name
        old = path.read_text() if path.exists() else ""
        path.write_text(old + "".join(f"# change {minute} {i}\n" for i in range(lines)))
        git("add", name)
        git("commit", "-q", "-m", f"Change {name}", minute=minute)
    for session, cwd, model, effort, minutes, edits, output in SESSIONS:
        lines = []
        for n, minute in enumerate(minutes):
            tool = {"name": "Edit" if edits and n % 2 else "Read", "input": {"n": n}}
            lines.append(
                {
                    "effort": effort,
                    "type": "assistant",
                    "sessionId": session,
                    "cwd": str(cwd),
                    "timestamp": f"2026-10-14T{9 + minute // 60:02d}:{minute % 60:02d}:00.000Z",
                    "isSidechain": False,
                    "message": {
                        "id": f"msg_{session[:4]}_{n:03d}",
                        "model": model,
                        "role": "assistant",
                        "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": f"toolu_{session[:4]}_{n}", **tool}],
                        "usage": {
                            "input_tokens": 3,
                            "output_tokens": output,
                            "cache_read_input_tokens": 30_000 + n * 3_000,
                            "cache_creation_input_tokens": 2_000,
                        },
                    },
                }
            )
        path = TRANSCRIPTS / "example" / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    print(f"wrote {REPO} and transcripts under {TRANSCRIPTS}")


if __name__ == "__main__":
    main()
