"""Regenerate examples/nudges/c0ffee00-long.jsonl: a long session for the live-nudge examples.

Nine prompts whose context grows from 20k to 180k tokens, cached with the 1-hour TTL, so the
`statusline` and `hook UserPromptSubmit` examples in README.md cross the kitchen-sink threshold.
"""

import json
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "examples" / "nudges" / "c0ffee00-long.jsonl"
SESSION = "c0ffee00-0000-4000-8000-000000000001"


def main() -> None:
    lines: list[dict[str, object]] = []
    for n in range(9):
        ts = f"2026-10-15T10:{n * 5:02d}:00.000Z"
        context = 20_000 + n * 20_000
        lines.append(
            {
                "type": "user",
                "sessionId": SESSION,
                "cwd": "/home/dev/shop",
                "timestamp": ts,
                "message": {"role": "user", "content": f"prompt {n + 1}"},
            }
        )
        lines.append(
            {
                "type": "assistant",
                "sessionId": SESSION,
                "cwd": "/home/dev/shop",
                "timestamp": ts,
                "isSidechain": False,
                "message": {
                    "id": f"msg_c0ff_{n:03d}",
                    "model": "claude-opus-5-5",
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1_200,
                        "cache_read_input_tokens": context - 2_000,
                        "cache_creation_input_tokens": 2_000,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": 2_000,
                            "ephemeral_5m_input_tokens": 0,
                        },
                    },
                },
            }
        )
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


if __name__ == "__main__":
    main()
