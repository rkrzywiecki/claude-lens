"""LLM coach: Claude turns the insights report into a short, prioritized plan.

Opt-in, because it calls the Claude API (and costs a few cents). Only aggregates leave the
machine: the usage profile, each insight's title, finding, recommendation, savings and
confidence, and the practice scorecard. Session ids, project paths and evidence lists are not
sent, and claude-lens never stores prompt content to begin with. `coach_payload` returns exactly
what would be sent, for `--dry-run`.

Credentials come from the Anthropic SDK's own resolution (ANTHROPIC_API_KEY, an auth token, or
an `ant auth login` profile).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from claude_lens.insights.models import InsightsReport
from claude_lens.models import Usage
from claude_lens.pricing import Pricing, normalize_model

if TYPE_CHECKING:
    import anthropic

# Models that accept server-side fallbacks on refusal ("default" routes by refusal category).
_FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 8_000

SYSTEM_PROMPT = """\
You are an efficiency coach for people who use Claude: Claude Code for software work, and the \
Claude API in their own applications. You receive a measured usage profile, the findings of a \
rule-based analyzer, and a best-practice scorecard, all computed from the user's own logs.

Write a short, practical report that helps the user get more done for fewer tokens:
- Start with the three changes that matter most, ordered by impact. For each: what to change, \
why (cite the numbers you were given), the expected effect, and how to try it (the exact \
command, setting or habit).
- Then a section on models and effort: which model and effort level fit which kind of work, \
based on the profile.
- Then a short section on what the user already does well.
- End with one line on what to measure next time to confirm the changes helped.

Rules:
- Use only the numbers in the input. Never invent figures, sessions or features.
- Savings are API-equivalent list prices. "reprice" means the same tokens priced on another \
model, "estimate" uses a stated assumption, "upper_bound" is the most that could be saved. Say \
which one a number is.
- The logs show cost, not quality. Present cheaper models and lower effort as experiments to \
try on real tasks, never as guaranteed wins. Recommend raising effort or model where the data \
shows struggling.
- Low-confidence findings are hypotheses; say so.
- Be concise: at most about 450 words. Use Markdown headings and bullet points.
"""


class CoachError(RuntimeError):
    """The coach could not produce a report (credentials, API error, refusal)."""


class CoachResult(BaseModel):
    text: str
    model: str
    usage: Usage
    cost: float | None


def coach_payload(report: InsightsReport, language: str = "English") -> dict[str, Any]:
    """The data sent to the model: aggregates only, no ids, paths or evidence."""
    p = report.profile
    return {
        "language": language,
        "profile": {
            "range": {
                "since": p.since.date().isoformat() if p.since else None,
                "until": p.until.date().isoformat() if p.until else None,
            },
            "sessions": p.sessions,
            "requests": p.requests,
            "api_equivalent_cost_usd": round(p.cost, 2),
            "claude_code_cost_usd": round(p.claude_code_cost, 2),
            "api_apps_cost_usd": round(p.api_cost, 2),
            "cost_by_model": {m.key: round(m.share, 3) for m in p.models},
            "cost_by_effort": {m.key: round(m.share, 3) for m in p.efforts},
            "cost_by_interface": {m.key: round(m.share, 3) for m in p.entrypoints},
            "thinking_share_of_output": _round(p.thinking_share),
            "subagent_share_of_cost": _round(p.subagent_cost_share),
            "cache_hit_ratio": _round(p.cache_hit_ratio),
            "median_requests_per_session": p.median_session_requests,
            "p95_peak_context_tokens": p.p95_peak_prompt_tokens,
        },
        "findings": [
            {
                "id": i.id,
                "category": i.category,
                "severity": i.severity,
                "confidence": i.confidence,
                "title": i.title,
                "finding": i.finding,
                "recommendation": i.recommendation,
                "savings_usd": i.savings_usd,
                "savings_kind": i.savings_kind,
                "sessions_affected": len(i.sessions) or None,
            }
            for i in report.insights
        ],
        "practices": [
            {"practice": pr.title, "status": pr.status, "evidence": pr.evidence}
            for pr in report.practices
        ],
    }


def run_coach(
    report: InsightsReport,
    pricing: Pricing,
    model: str,
    effort: str = "medium",
    language: str = "English",
    client: anthropic.Anthropic | None = None,
    timeout: float = 120.0,
) -> CoachResult:
    import anthropic

    client = client or anthropic.Anthropic(timeout=timeout, max_retries=2)
    if not (client.api_key or client.auth_token or client.credentials is not None):
        raise CoachError("no Claude API credentials: set ANTHROPIC_API_KEY or run `ant auth login`")
    payload = coach_payload(report, language)
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "output_config": {"effort": effort},
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Write the report in {language}. Here is the analysis as JSON:\n\n"
                    + json.dumps(payload, indent=1)
                ),
            }
        ],
    }
    if normalize_model(model) in _FALLBACK_MODELS:
        request["betas"] = [FALLBACK_BETA]
        request["fallbacks"] = "default"
    try:
        message = client.beta.messages.create(**request)
    except anthropic.AuthenticationError as exc:
        raise CoachError("the Claude API rejected the credentials") from exc
    except anthropic.NotFoundError as exc:
        raise CoachError(f"model {model!r} not found (LENS_COACH_MODEL)") from exc
    except anthropic.RateLimitError as exc:
        raise CoachError("rate limited by the Claude API; try again later") from exc
    except anthropic.APIStatusError as exc:
        raise CoachError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise CoachError("could not reach the Claude API") from exc
    if message.stop_reason == "refusal":
        raise CoachError("the model declined to write the report")
    text = "\n".join(b.text for b in message.content if b.type == "text").strip()
    if not text:
        raise CoachError("the model returned no text")
    usage = Usage(
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        cache_read_tokens=message.usage.cache_read_input_tokens or 0,
        cache_write_5m_tokens=message.usage.cache_creation_input_tokens or 0,
    )
    cost = pricing.cost(message.model, usage)
    return CoachResult(
        text=text, model=message.model, usage=usage, cost=cost.total if cost else None
    )


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None
