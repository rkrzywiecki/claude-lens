"""Data models of the insights report."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field

Category = Literal["model", "effort", "context", "cache", "workflow", "setup", "api"]
Severity = Literal["high", "medium", "low", "info"]
Confidence = Literal["high", "medium", "low"]
Scope = Literal["claude_code", "api", "all"]
PracticeStatus = Literal["pass", "warn", "fail", "n/a"]
# How a saving was computed: the same tokens repriced (`reprice`), an estimate with an assumed
# reduction (`estimate`), or the most that could be saved (`upper_bound`).
SavingsKind = Literal["reprice", "estimate", "upper_bound"]


class Insight(BaseModel):
    """One recommendation, with the evidence it rests on."""

    id: str
    category: Category
    scope: Scope
    severity: Severity
    confidence: Confidence
    title: str
    finding: str
    recommendation: str
    evidence: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)
    savings_usd: float | None = None
    savings_kind: SavingsKind | None = None
    source: str | None = None


class Practice(BaseModel):
    """A documented best practice and whether the recorded activity follows it."""

    id: str
    title: str
    scope: Scope
    status: PracticeStatus
    evidence: str
    source: str


class Mix(BaseModel):
    """A share of spend or requests per key (model, effort, entrypoint)."""

    key: str
    requests: int
    cost: float
    share: float


class Profile(BaseModel):
    """How Claude was used in the selected range: the numbers the insights are drawn from."""

    since: datetime | None
    until: datetime | None
    sessions: int
    requests: int
    cost: float
    claude_code_cost: float
    api_cost: float
    models: list[Mix]
    efforts: list[Mix]
    entrypoints: list[Mix]
    thinking_share: float | None
    subagent_cost_share: float | None
    cache_hit_ratio: float | None
    median_session_requests: float | None
    p95_peak_prompt_tokens: int | None


class InsightsReport(BaseModel):
    profile: Profile
    insights: list[Insight]
    practices: list[Practice]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_savings_usd(self) -> float:
        """Sum of the estimated savings; overlapping insights can count the same tokens twice."""
        return sum(i.savings_usd or 0.0 for i in self.insights)
