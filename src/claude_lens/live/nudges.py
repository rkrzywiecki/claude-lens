"""`claude-lens hook UserPromptSubmit`: nudges before context waste happens.

Claude Code runs the hook with JSON on stdin before each prompt is processed; see
https://code.claude.com/docs/en/hooks. The hook never blocks a prompt and never adds anything
to Claude's context. When the session has crossed one of the insight thresholds
(`insights/thresholds.py`), it returns a `systemMessage`, which Claude Code shows to the user:

- `context.kitchen_sink`: KITCHEN_SINK_PROMPTS prompts and KITCHEN_SINK_PEAK_TOKENS of context.
  Suggests `/rename` + `/clear` for an unrelated task, or `/compact <focus>` for the same one.
- `context.near_limit`: the context fills NEAR_LIMIT_SHARE of the model's window.

Each nudge fires at most once per session (`/clear` starts a new session id); what fired is
kept in `LENS_STATE_DIR/nudges/`. Any error means no output and exit code 0.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from claude_lens.insights.thresholds import (
    KITCHEN_SINK_PEAK_TOKENS,
    KITCHEN_SINK_PROMPTS,
    NEAR_LIMIT_SHARE,
)
from claude_lens.live.session import LiveSession, SessionSnapshot, prune, state_dir
from claude_lens.live.statusline import tokens_label

SUPPORTED_EVENTS = frozenset({"UserPromptSubmit"})


class HookInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str | None = None
    transcript_path: str | None = None
    hook_event_name: str | None = None


class Nudge(BaseModel):
    id: str
    message: str


class NudgeState(BaseModel):
    fired: list[str] = Field(default_factory=list)


def due_nudges(snapshot: SessionSnapshot) -> list[Nudge]:
    """Nudges whose threshold the session has crossed, most urgent first."""
    nudges: list[Nudge] = []
    tokens = tokens_label(snapshot.context_tokens)
    share = snapshot.context_share
    if share is not None and share >= NEAR_LIMIT_SHARE and snapshot.context_window:
        nudges.append(
            Nudge(
                id="context.near_limit",
                message=(
                    f"Context is {share:.0%} of the model's window ({tokens} of "
                    f"{tokens_label(snapshot.context_window)} tokens); quality drops as it "
                    "fills. Run `/compact <what to keep>`, or write a short spec and continue "
                    "in a fresh session."
                ),
            )
        )
    if (
        snapshot.human_prompts >= KITCHEN_SINK_PROMPTS
        and snapshot.context_tokens >= KITCHEN_SINK_PEAK_TOKENS
    ):
        nudges.append(
            Nudge(
                id="context.kitchen_sink",
                message=(
                    f"This session carries {tokens} tokens of context after "
                    f"{snapshot.human_prompts} prompts, and every request re-reads it. New, "
                    "unrelated task? `/rename` the session, then `/clear` (come back with "
                    "`/resume`). Same task? `/compact <what to keep>`."
                ),
            )
        )
    return nudges


def respond(stdin_text: str, event: str, root: Path | None = None) -> str:
    """Hook stdout for one run: a JSON object, or '' when there is nothing to say."""
    if event not in SUPPORTED_EVENTS:
        return ""
    try:
        data = HookInput.model_validate(json.loads(stdin_text or "{}"))
    except (ValueError, ValidationError):
        return ""
    if not data.transcript_path or not data.session_id:
        return ""
    root = root or state_dir()
    session = LiveSession.open(Path(data.transcript_path), root)
    snapshot = session.snapshot()
    session.save()

    state_file = root / "nudges" / f"{_safe(data.session_id)}.json"
    state = _load(state_file)
    fresh = [n for n in due_nudges(snapshot) if n.id not in state.fired]
    if not fresh:
        return ""
    state.fired.extend(n.id for n in fresh)
    _save(state_file, state)
    message = "claude-lens: " + " ".join(n.message for n in fresh)
    return json.dumps({"systemMessage": message})


def main(event: str) -> int:
    try:
        output = respond(sys.stdin.read(), event)
    except Exception:  # a hook must never get in the way of a prompt
        output = ""
    if output:
        print(output)
    return 0


def _load(path: Path) -> NudgeState:
    try:
        return NudgeState.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return NudgeState()


def _save(path: Path, state: NudgeState) -> None:
    try:
        is_new = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(state.model_dump_json(), encoding="utf-8")
        tmp.replace(path)
        if is_new:
            prune(path.parent)
    except OSError:
        return


def _safe(session_id: str) -> str:
    """Session ids are UUIDs; anything else is reduced to a safe file name."""
    cleaned = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64]
    return cleaned or "unknown"
