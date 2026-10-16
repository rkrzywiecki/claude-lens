"""Data models shared by importers, the store and reports."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

Source = Literal["claude_code", "proxy"]
Speed = Literal["standard", "fast"]
GroupBy = Literal["day", "project", "model", "session"]


class Usage(BaseModel):
    """Token counts of one API response, as reported in `usage`."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_write_tokens(self) -> int:
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @computed_field  # type: ignore[prop-decorator]
    @property
    def prompt_tokens(self) -> int:
        """Every input token of the request: uncached, written to cache and read from it."""
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_5m_tokens=self.cache_write_5m_tokens + other.cache_write_5m_tokens,
            cache_write_1h_tokens=self.cache_write_1h_tokens + other.cache_write_1h_tokens,
        )


class RequestFeatures(BaseModel):
    """Request settings the proxy saw; none of them carries prompt content."""

    stream: bool = False
    max_tokens: int | None = None
    tool_count: int = 0
    # Whether any block, tool or the request itself carries cache_control.
    cache_control: bool = False
    thinking: str | None = None
    # Text characters in the system prompt (a size, not the text).
    system_chars: int = 0


class RequestRecord(BaseModel):
    """One model response (one API request), whatever source it came from."""

    message_id: str
    request_id: str | None = None
    session_id: str
    project: str
    model: str
    timestamp: datetime
    usage: Usage
    speed: Speed = "standard"
    stop_reason: str | None = None
    tool_names: list[str] = Field(default_factory=list)
    is_sidechain: bool = False
    source: Source = "claude_code"
    # Measured by the proxy only (milliseconds from receiving the request).
    ttft_ms: float | None = None
    latency_ms: float | None = None
    # Proxy only: content-free hash of each prompt segment (see fingerprint.py).
    segments: list[str] | None = None
    # Effort the request ran at (low ... max), when known.
    effort: str | None = None
    # Part of output_tokens spent on thinking, when the API reports it.
    thinking_tokens: int | None = None
    # Subagent that made the request (Claude Code transcripts).
    agent_id: str | None = None
    # Proxy only: request settings relevant to cost (no content).
    features: RequestFeatures | None = None


class SessionMeta(BaseModel):
    """What one transcript file says about how a session was run (Claude Code only).

    Kept per file and summed when read, so re-importing a file never double-counts.
    """

    session_id: str
    source_file: str
    entrypoint: str | None = None
    version: str | None = None
    permission_modes: list[str] = Field(default_factory=list)
    human_prompts: int = 0
    interruptions: int = 0
    tool_denials: int = 0
    api_errors: int = 0
    skills: list[str] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    active_ms: int = 0


class ToolCall(BaseModel):
    """One tool_use block; only a hash of its input is kept, plus the file it targets."""

    tool_use_id: str
    message_id: str
    session_id: str
    timestamp: datetime
    tool_name: str
    input_hash: str
    target: str | None = None
    # From the matching tool_result: None until the result is seen.
    is_error: bool | None = None
    source: Source = "claude_code"


class CostBreakdown(BaseModel):
    """API-equivalent cost in USD, split by token kind."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    # What the same tokens would have cost with no prompt caching at all.
    without_cache: float = 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> float:
        return self.input + self.output + self.cache_read + self.cache_write

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_savings(self) -> float:
        return self.without_cache - self.total

    def __add__(self, other: CostBreakdown) -> CostBreakdown:
        return CostBreakdown(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            without_cache=self.without_cache + other.without_cache,
        )


class ReportRow(BaseModel):
    """Aggregated usage and cost for one group (a day, project, model or session)."""

    key: str
    label: str | None = None
    requests: int = 0
    usage: Usage = Field(default_factory=Usage)
    cost: CostBreakdown = Field(default_factory=CostBreakdown)
    unpriced_requests: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_hit_ratio(self) -> float | None:
        """Share of prompt tokens served from cache; None when there were none."""
        total = self.usage.prompt_tokens
        return self.usage.cache_read_tokens / total if total else None


class Report(BaseModel):
    group_by: GroupBy
    rows: list[ReportRow]
    total: ReportRow
    unpriced_models: list[str] = Field(default_factory=list)


class ImportStats(BaseModel):
    files_seen: int = 0
    files_imported: int = 0
    files_skipped: int = 0
    records: int = 0
    tool_calls: int = 0
    bad_lines: int = 0
