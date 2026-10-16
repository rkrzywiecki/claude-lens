"""LLM coach: Claude turns the insights report into a short, prioritized plan.

Opt-in, because it sends data to Claude. Only aggregates leave the machine: the usage profile,
each insight's title, finding, recommendation, savings and confidence, and the practice
scorecard. Session ids, project paths and evidence lists are not sent, and claude-lens never
stores prompt content to begin with. `coach_payload` returns exactly what would be sent, for
`--dry-run`.

Two backends:

- `api`: the Claude API through the Anthropic SDK. Credentials come from the SDK's own
  resolution (ANTHROPIC_API_KEY, an auth token, or an `ant auth login` profile); billed per
  token, a few cents per report.
- `claude-code`: the local Claude Code CLI in print mode (`claude -p`), with all tools off, a
  fixed system prompt, no saved session, run in an empty temporary directory so no project
  CLAUDE.md or project settings load. It uses whatever Claude Code is signed in with, so a
  claude.ai subscription works without an API key (the request counts against the plan).

`auto` picks `api` when the SDK finds credentials, else `claude-code` when the CLI is on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from claude_lens.insights.models import Comparison, InsightsReport, PeriodMetrics
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
- When the input has `since_last_period`, add a short section on what improved and what got \
worse against the previous range of the same length, citing those numbers. A `verdict` of \
"better" or "worse" follows the metric's `better_when`; "same" means no real change.
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


Backend = Literal["auto", "api", "claude-code"]


class CoachError(RuntimeError):
    """The coach could not produce a report (credentials, API error, refusal)."""


class CoachResult(BaseModel):
    text: str
    model: str
    backend: Literal["api", "claude-code"]
    usage: Usage
    # API-equivalent cost; on a subscription (claude-code backend) nothing is billed per token.
    cost: float | None


def coach_payload(report: InsightsReport, language: str = "English") -> dict[str, Any]:
    """The data sent to the model: aggregates only, no ids, paths or evidence."""
    p = report.profile
    payload: dict[str, Any] = {
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
    if report.compare:
        payload["since_last_period"] = _comparison(report.compare)
    return payload


def _comparison(comparison: Comparison) -> dict[str, Any]:
    """This range against the one before it: aggregates only, like the rest of the payload."""

    def period(m: PeriodMetrics) -> dict[str, Any]:
        return {
            "since": m.since.astimezone().date().isoformat(),
            "until": (m.until.astimezone() - timedelta(seconds=1)).date().isoformat(),
            "sessions": m.sessions,
            "requests": m.requests,
            "cost_by_model": {x.key: round(x.share, 3) for x in m.models},
            "cost_by_effort": {x.key: round(x.share, 3) for x in m.efforts},
        }

    return {
        "current": period(comparison.current),
        "previous": period(comparison.previous),
        "metrics": [
            {
                "metric": c.label,
                "unit": c.unit,
                "previous": _round(c.previous),
                "current": _round(c.current),
                "better_when": c.better,
                "verdict": c.verdict,
            }
            for c in comparison.changes
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
    backend: Backend = "auto",
    claude_bin: str = "claude",
) -> CoachResult:
    prompt = user_prompt(report, language)
    chosen = backend
    if backend == "auto":
        if client is None:
            client = _sdk_client(timeout)
        if _has_credentials(client):
            chosen = "api"
        elif shutil.which(claude_bin):
            chosen = "claude-code"
        else:
            raise CoachError(
                "no Claude API credentials (ANTHROPIC_API_KEY or `ant auth login`) and no "
                f"`{claude_bin}` CLI on PATH to use a Claude Code login instead"
            )
    if chosen == "claude-code":
        return _run_claude_code(prompt, pricing, model, effort, timeout, claude_bin)
    return _run_api(prompt, pricing, model, effort, client or _sdk_client(timeout))


def user_prompt(report: InsightsReport, language: str) -> str:
    return f"Write the report in {language}. Here is the analysis as JSON:\n\n" + json.dumps(
        coach_payload(report, language), indent=1
    )


def _sdk_client(timeout: float) -> anthropic.Anthropic:
    import anthropic

    return anthropic.Anthropic(timeout=timeout, max_retries=2)


def _has_credentials(client: anthropic.Anthropic) -> bool:
    return bool(client.api_key or client.auth_token or client.credentials is not None)


def _run_api(
    prompt: str, pricing: Pricing, model: str, effort: str, client: anthropic.Anthropic
) -> CoachResult:
    import anthropic

    if not _has_credentials(client):
        raise CoachError("no Claude API credentials: set ANTHROPIC_API_KEY or run `ant auth login`")
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "output_config": {"effort": effort},
        "messages": [{"role": "user", "content": prompt}],
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
        text=text,
        model=message.model,
        backend="api",
        usage=usage,
        cost=cost.total if cost else None,
    )


def _run_claude_code(
    prompt: str, pricing: Pricing, model: str, effort: str, timeout: float, claude_bin: str
) -> CoachResult:
    command = [
        claude_bin,
        "-p",
        "--output-format",
        "json",
        "--tools",
        "",
        "--system-prompt",
        SYSTEM_PROMPT,
        "--model",
        model,
        "--effort",
        effort,
        "--no-session-persistence",
    ]
    # An empty working directory: no project CLAUDE.md, settings or hooks get involved.
    with tempfile.TemporaryDirectory(prefix="claude-lens-coach-") as cwd:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CoachError(f"`{claude_bin}` CLI not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise CoachError(f"Claude Code did not answer within {timeout:.0f} s") from exc
    try:
        result = json.loads(done.stdout)
    except ValueError:
        detail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
        raise CoachError(f"Claude Code failed (exit {done.returncode}): {detail[0]}") from None
    if not isinstance(result, dict):
        raise CoachError("Claude Code returned unexpected output")
    text = result.get("result")
    if result.get("is_error") or done.returncode != 0:
        raise CoachError(f"Claude Code reported an error: {text or result.get('subtype')}")
    if not isinstance(text, str) or not text.strip():
        raise CoachError("the model returned no text")
    raw_usage = result.get("usage")
    raw: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    usage = Usage(
        input_tokens=_count(raw.get("input_tokens")),
        output_tokens=_count(raw.get("output_tokens")),
        cache_read_tokens=_count(raw.get("cache_read_input_tokens")),
        cache_write_5m_tokens=_count(raw.get("cache_creation_input_tokens")),
    )
    models = result.get("modelUsage")
    used = next(iter(models), model) if isinstance(models, dict) and models else model
    reported = result.get("total_cost_usd")
    if isinstance(reported, int | float):
        cost: float | None = float(reported)
    else:
        priced = pricing.cost(used, usage)
        cost = priced.total if priced else None
    return CoachResult(text=text.strip(), model=used, backend="claude-code", usage=usage, cost=cost)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None
