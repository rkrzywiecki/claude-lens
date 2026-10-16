"""`claude-lens install`: add the status line and the prompt hook to a Claude Code settings.json.

Existing settings are preserved. Only hook entries whose command runs `claude-lens hook` are
replaced; other hooks, permissions and env are left as they are. A status line that is not
claude-lens's own is kept unless `force` is set (Claude Code has a single status line). The
previous file is copied to `settings.json.bak-<timestamp>` before it changes, and a file that is
not valid JSON is never touched.
"""

from __future__ import annotations

import json
import shlex
import shutil
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, JsonValue

HOOK_MARKER = "claude-lens hook"
STATUSLINE_MARKER = "claude-lens statusline"
HOOK_EVENT = "UserPromptSubmit"
HOOK_TIMEOUT_SECONDS = 10
# Re-run the status line on a timer too, so the cache countdown moves while the session idles.
STATUSLINE_REFRESH_SECONDS = 30

JsonObject = dict[str, JsonValue]


class InstallError(Exception):
    """The settings file cannot be updated safely."""


class InstallResult(BaseModel):
    path: Path
    # False when the settings already held exactly this configuration.
    changed: bool = False
    backup: Path | None = None
    statusline: bool = False
    hook: bool = False
    # Set when an existing status line was kept instead of replaced.
    kept_statusline: str | None = None


def lens_root() -> Path:
    """The claude-lens project directory (where pyproject.toml lives)."""
    return Path(__file__).resolve().parents[3]


def settings_file(user: bool, cwd: Path | None = None) -> Path:
    if user:
        return Path.home() / ".claude" / "settings.json"
    return (cwd or Path.cwd()) / ".claude" / "settings.json"


def launcher(root: Path) -> str:
    """How settings.json starts claude-lens: through uv in a checkout, else the installed script."""
    if (root / "pyproject.toml").is_file():
        return f"uv run --quiet --project {shlex.quote(str(root))} claude-lens"
    found = shutil.which("claude-lens")
    if found is None:
        raise InstallError("cannot find the claude-lens executable on PATH")
    return shlex.quote(found)


def merge_settings(
    existing: JsonObject,
    command: str,
    *,
    statusline: bool = True,
    hook: bool = True,
    force: bool = False,
) -> tuple[JsonObject, InstallResult]:
    """Return `existing` with the claude-lens status line and hook added or replaced.

    The result's `path` is a placeholder; `install` fills it in.
    """
    merged = dict(existing)
    result = InstallResult(path=Path())

    if statusline:
        current = merged.get("statusLine")
        current_command = current.get("command") if isinstance(current, dict) else None
        ours = isinstance(current_command, str) and STATUSLINE_MARKER in current_command
        if current is None or ours or force:
            merged["statusLine"] = {
                "type": "command",
                "command": f"{command} statusline",
                "padding": 0,
                "refreshInterval": STATUSLINE_REFRESH_SECONDS,
            }
            result.statusline = True
        else:
            result.kept_statusline = (
                current_command if isinstance(current_command, str) else json.dumps(current)
            )

    if hook:
        hooks_value = merged.get("hooks", {})
        if not isinstance(hooks_value, dict):
            raise InstallError('"hooks" in settings.json is not an object')
        hooks: JsonObject = dict(hooks_value)
        groups_value = hooks.get(HOOK_EVENT, [])
        if not isinstance(groups_value, list):
            raise InstallError(f'"hooks.{HOOK_EVENT}" in settings.json is not a list')
        groups: list[JsonValue] = [
            g for g in (_without_lens(group) for group in groups_value) if g is not None
        ]
        groups.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{command} hook {HOOK_EVENT}",
                        "timeout": HOOK_TIMEOUT_SECONDS,
                    }
                ]
            }
        )
        hooks[HOOK_EVENT] = groups
        merged["hooks"] = hooks
        result.hook = True
    return merged, result


def install(
    path: Path,
    *,
    statusline: bool = True,
    hook: bool = True,
    force: bool = False,
    root: Path | None = None,
) -> InstallResult:
    """Write merged settings to `path`, backing up the previous file."""
    existing: JsonObject = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise InstallError(f"{path} is not valid JSON; fix it first ({exc})") from exc
        if not isinstance(loaded, dict):
            raise InstallError(f"{path} must contain a JSON object")
        existing = loaded

    command = launcher(root or lens_root())
    merged, result = merge_settings(
        existing, command, statusline=statusline, hook=hook, force=force
    )
    result.path = path
    if merged == existing:
        return result
    if path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        result.backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, result.backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    result.changed = True
    return result


def _without_lens(group: JsonValue) -> JsonValue:
    """Drop claude-lens commands from a matcher group; None if nothing is left."""
    if not isinstance(group, dict):
        return group
    commands = group.get("hooks")
    if not isinstance(commands, list):
        return group
    kept = [
        hook
        for hook in commands
        if not (isinstance(hook, dict) and HOOK_MARKER in str(hook.get("command", "")))
    ]
    if not kept:
        return None
    return {**group, "hooks": kept}
