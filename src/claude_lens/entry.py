"""Console script: a fast path for the commands Claude Code runs in the middle of a session.

`claude-lens statusline` and `claude-lens hook <event>` run after every message and before
every prompt. Importing the full CLI (Typer, rich, structlog, the analysis code) takes longer
than the whole status line budget, so those two exact invocations skip it. Everything else,
including `--help` on them, goes through the Typer app in `cli.py`.
"""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    if args == ["statusline"]:
        from claude_lens.live.statusline import main as statusline

        sys.exit(statusline())
    if len(args) == 2 and args[0] == "hook":
        from claude_lens.live.nudges import main as hook

        sys.exit(hook(args[1]))
    from claude_lens.cli import app

    app()
