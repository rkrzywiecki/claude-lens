"""Regenerate the synthetic transcripts in examples/transcripts (used by README examples)."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "examples" / "transcripts"


def entry(session: str, cwd: str, n: int, ts: str, model: str, **usage: int) -> dict[str, object]:
    return {
        "type": "assistant",
        "sessionId": session,
        "cwd": cwd,
        "timestamp": ts,
        "isSidechain": False,
        "message": {
            "id": f"msg_{session[:4]}_{n:03d}",
            "model": model,
            "role": "assistant",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": f"toolu_{n}", "name": "Bash", "input": {}}],
            "usage": {"input_tokens": 3, **usage},
        },
    }


def main() -> None:
    sessions = {
        # Well-cached session: a stable prefix, so most prompt tokens are cache reads.
        ("1f0c9a52-cached", "/home/dev/shop", "-home-dev-shop", "claude-opus-5-5", True),
        # Cache-busting session: the prefix changes every turn, so it is rewritten each time.
        ("7b2e4d10-busted", "/home/dev/blog", "-home-dev-blog", "claude-sonnet-5-5", False),
    }
    for session, cwd, project_dir, model, cached in sorted(sessions):
        lines = []
        context = 20_000
        for n in range(12):
            ts = f"2026-10-1{n // 6 + 4}T09:{n * 4:02d}:00.000Z"
            if cached:
                usage = {"cache_read_input_tokens": context, "cache_creation_input_tokens": 1_500}
            else:
                usage = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": context}
            lines.append(entry(session, cwd, n, ts, model, output_tokens=800, **usage))
            context += 2_000
        path = ROOT / project_dir / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    print(f"wrote transcripts under {ROOT}")


if __name__ == "__main__":
    main()
