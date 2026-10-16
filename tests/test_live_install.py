"""`claude-lens install`: merging the status line and hook into Claude Code settings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from claude_lens.cli import app
from claude_lens.live.install import (
    HOOK_EVENT,
    InstallError,
    install,
    launcher,
    merge_settings,
)

COMMAND = launcher(Path(__file__).resolve().parents[1])


def read(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def lens_hooks(settings: dict[str, Any]) -> list[str]:
    return [
        h["command"]
        for group in settings["hooks"][HOOK_EVENT]
        for h in group["hooks"]
        if "claude-lens hook" in h["command"]
    ]


def test_launcher_quotes_the_checkout_path(tmp_path: Path) -> None:
    root = tmp_path / "my lens"
    root.mkdir()
    (root / "pyproject.toml").write_text("")
    assert launcher(root) == f"uv run --quiet --project '{root}' claude-lens"


def test_install_creates_settings(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    result = install(path)
    assert result.changed and result.backup is None
    settings = read(path)
    assert settings["statusLine"]["command"] == f"{COMMAND} statusline"
    assert settings["statusLine"]["type"] == "command"
    assert settings["statusLine"]["refreshInterval"] >= 1
    assert lens_hooks(settings) == [f"{COMMAND} hook UserPromptSubmit"]


def test_install_keeps_other_settings_and_backs_up(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = {
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {
            HOOK_EVENT: [{"hooks": [{"type": "command", "command": "other-tool check"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "notify"}]}],
        },
    }
    path.write_text(json.dumps(original))
    result = install(path)
    assert result.backup is not None
    assert read(result.backup) == original
    settings = read(path)
    assert settings["permissions"] == original["permissions"]
    assert settings["hooks"]["Stop"] == original["hooks"]["Stop"]
    commands = [h["command"] for g in settings["hooks"][HOOK_EVENT] for h in g["hooks"]]
    assert commands[0] == "other-tool check"
    assert len(lens_hooks(settings)) == 1


def test_reinstall_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    install(path)
    first = path.read_text()
    again = install(path)
    assert not again.changed and again.backup is None
    assert path.read_text() == first
    assert list(tmp_path.glob("settings.json.bak-*")) == []


def test_reinstall_replaces_an_old_lens_hook(tmp_path: Path) -> None:
    old = {
        "hooks": {HOOK_EVENT: [{"hooks": [{"command": "/old/claude-lens hook UserPromptSubmit"}]}]}
    }
    merged, _ = merge_settings(old, "/new/claude-lens", statusline=False)
    assert lens_hooks(merged) == ["/new/claude-lens hook UserPromptSubmit"]
    assert len(merged["hooks"][HOOK_EVENT]) == 1  # type: ignore[index, arg-type]


def test_foreign_statusline_is_kept_unless_forced(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-line.sh"}}))
    result = install(path)
    assert not result.statusline and result.kept_statusline == "my-line.sh"
    assert read(path)["statusLine"]["command"] == "my-line.sh"
    assert result.hook

    forced = install(path, force=True)
    assert forced.statusline
    assert read(path)["statusLine"]["command"] == f"{COMMAND} statusline"


def test_partial_install(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    install(path, statusline=False)
    assert "statusLine" not in read(path)
    install(path, hook=False)
    assert "statusLine" in read(path)


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"hooks": []}'])
def test_unsafe_settings_are_never_touched(tmp_path: Path, content: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(content)
    with pytest.raises(InstallError):
        install(path)
    assert path.read_text() == content
    assert list(tmp_path.glob("settings.json.bak-*")) == []


def test_cli_install_user_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(app, ["install", "--user"])
    assert result.exit_code == 0, result.output
    path = tmp_path / "home" / ".claude" / "settings.json"
    assert "statusline" in read(path)["statusLine"]["command"]

    result = CliRunner().invoke(app, ["install", "--user"])
    assert "Already up to date" in result.output


def test_cli_install_reports_bad_json(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text("{oops")
    result = CliRunner().invoke(app, ["install"])
    assert result.exit_code == 2
    assert path.read_text() == "{oops"
