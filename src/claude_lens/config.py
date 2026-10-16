"""Settings read from the environment (prefix LENS_) and an optional .env file."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LENS_",
        env_file=".env",
        extra="ignore",
        env_ignore_empty=True,
    )

    db_path: Path = Path("~/.claude-lens/lens.db")
    transcripts_dir: Path = Path("~/.claude/projects")
    pricing_path: Path | None = None
    log_level: str = "WARNING"

    # Proxy (`claude-lens serve`).
    upstream_url: str = "https://api.anthropic.com"
    upstream_timeout: float = 600.0
    host: str = "127.0.0.1"
    port: int = 8788
    allow_non_loopback: bool = False

    # LLM coach (`claude-lens insights --coach`).
    coach_model: str = "claude-opus-5-5"
    coach_effort: str = "medium"
    coach_language: str = "English"
    coach_timeout: float = 120.0
    # auto: the API when the SDK finds credentials, else the Claude Code CLI (`claude -p`).
    coach_backend: Literal["auto", "api", "claude-code"] = "auto"
    coach_claude_bin: str = "claude"

    def resolved_db_path(self) -> Path:
        return self.db_path.expanduser()

    def resolved_transcripts_dir(self) -> Path:
        return self.transcripts_dir.expanduser()

    def resolved_pricing_path(self) -> Path | None:
        return self.pricing_path.expanduser() if self.pricing_path else None


def is_loopback(host: str) -> bool:
    """True for addresses reachable only from this machine (localhost, 127.0.0.0/8, ::1)."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
