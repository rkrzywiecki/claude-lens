"""Insight rules: each looks at the activity and returns recommendations with evidence.

Every threshold below is a heuristic, named so it can be found and tuned. Savings are
API-equivalent list prices and say how they were computed (`savings_kind`): a repricing of the
same tokens, an estimate with a stated assumption, or an upper bound. Logs show cost, not
quality, so a cheaper model or a lower effort is always a candidate to try, never a verdict.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from claude_lens.analysis.cache import CacheDoctorReport
from claude_lens.insights.models import Confidence, Insight, SavingsKind
from claude_lens.insights.stats import SessionStats, is_big_model

# Shared with the live nudges; re-exported so rules.X keeps working.
from claude_lens.insights.thresholds import KITCHEN_SINK_PEAK_TOKENS as KITCHEN_SINK_PEAK_TOKENS
from claude_lens.insights.thresholds import KITCHEN_SINK_PROMPTS as KITCHEN_SINK_PROMPTS
from claude_lens.insights.thresholds import NEAR_LIMIT_SHARE as NEAR_LIMIT_SHARE
from claude_lens.insights.whatif import reprice
from claude_lens.models import GroupBy, RequestRecord
from claude_lens.pricing import Pricing, normalize_model
from claude_lens.report import shorten_home

# Documentation the recommendations rest on.
DOCS = {
    "costs": "https://code.claude.com/docs/en/costs",
    "best_practices": "https://code.claude.com/docs/en/best-practices",
    "model_config": "https://code.claude.com/docs/en/model-config",
    "sub_agents": "https://code.claude.com/docs/en/sub-agents",
    "memory": "https://code.claude.com/docs/en/memory",
    "permission_modes": "https://code.claude.com/docs/en/permission-modes",
    "permissions": "https://code.claude.com/docs/en/permissions",
    "prompt_caching": "https://platform.claude.com/docs/en/build-with-claude/prompt-caching",
    "effort": "https://platform.claude.com/docs/en/build-with-claude/effort",
    "batch": "https://platform.claude.com/docs/en/build-with-claude/batch-processing",
    "streaming": "https://platform.claude.com/docs/en/build-with-claude/streaming",
    "models": "https://platform.claude.com/docs/en/about-claude/models/overview",
    "cost_intelligence": (
        "https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence"
    ),
}

# Cheaper models suggested for work that does not need the largest one.
CODING_ALTERNATIVE = "claude-sonnet-5-5"
LIGHT_ALTERNATIVE = "claude-haiku-5-5"

MIN_SESSION_COST = 0.05  # sessions cheaper than this are not worth a recommendation
LIGHT_SESSION_MIN_REQUESTS = 3
HIGH_EFFORTS = frozenset({"high", "xhigh", "max"})
THINKING_HEAVY_SHARE = 0.4
# Assumed share of thinking tokens a lower effort saves; only an order of magnitude.
EFFORT_THINKING_REDUCTION = 0.5
CONTEXT_BASELINE_TOKENS = 50_000
STRUGGLE_INTERRUPTIONS = 3
PLAN_MODE_MIN_FILES = 5
EXPLORATION_MIN_READS = 25
CLAUDE_MD_MAX_LINES = 200
CLAUDE_MD_MIN_SESSIONS = 3
# Scratch and temporary directories are not projects anyone maintains a CLAUDE.md for.
# Path prefixes only, compared as text; nothing is written there.
TEMP_ROOTS = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")  # noqa: S108
API_SMALL_OUTPUT = 300
API_CACHE_MIN_PROMPT = 4_096
API_LARGE_MAX_TOKENS = 16_000
API_BATCH_MIN_REQUESTS = 50
API_BATCH_DISCOUNT = 0.5
# Outcomes (git commits). Must match outcomes.GRACE.
OUTCOME_GRACE_MINUTES = 30
NO_COMMIT_MIN_COST = 2.0
OUTCOME_MIN_SESSIONS = 5
OUTCOME_MIN_COMMITS = 5
OUTCOME_COST_RATIO = 1.5


class RuleContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sessions: list[SessionStats]
    records: list[RequestRecord]
    cache: CacheDoctorReport
    pricing: Pricing
    total_cost: float
    # Whether to look at project directories on disk (CLAUDE.md checks).
    check_files: bool = True


Rule = Callable[[RuleContext], list[Insight]]


def severity(savings: float | None, total: float) -> str:
    if not savings or savings <= 0:
        return "info"
    share = savings / total if total else 0.0
    if savings >= 5 or share >= 0.05:
        return "high"
    if savings >= 1 or share >= 0.01:
        return "medium"
    return "low"


def insight(
    ctx: RuleContext,
    *,
    id: str,  # noqa: A002 - mirrors the model field
    category: str,
    scope: str,
    title: str,
    finding: str,
    recommendation: str,
    source: str,
    sessions: list[SessionStats] | None = None,
    evidence: list[str] | None = None,
    savings: float | None = None,
    kind: SavingsKind | None = None,
    confidence: Confidence = "medium",
    level: str | None = None,
) -> Insight:
    picked = sorted(sessions or [], key=lambda s: -s.cost)
    return Insight.model_validate(
        {
            "id": id,
            "category": category,
            "scope": scope,
            "severity": level or severity(savings, ctx.total_cost),
            "confidence": confidence,
            "title": title,
            "finding": finding,
            "recommendation": recommendation,
            "evidence": evidence
            or [f"{s.label}  {shorten_home(s.project)}  ${s.cost:,.2f}" for s in picked[:5]],
            "sessions": [s.session_id for s in picked],
            "savings_usd": round(savings, 2) if savings else None,
            "savings_kind": kind if savings else None,
            "source": source,
        }
    )


def claude_code_sessions(ctx: RuleContext) -> list[SessionStats]:
    return [s for s in ctx.sessions if s.source == "claude_code"]


def is_long_session(s: SessionStats) -> bool:
    return s.human_prompts >= KITCHEN_SINK_PROMPTS


def is_kitchen_sink(s: SessionStats) -> bool:
    """A long session that grew past the peak threshold without a context reset."""
    return is_long_session(s) and s.peak_prompt_tokens >= KITCHEN_SINK_PEAK_TOKENS and s.resets == 0


def is_large_change(s: SessionStats) -> bool:
    return len(s.edited_files) >= PLAN_MODE_MIN_FILES


def has_rework(s: SessionStats) -> bool:
    """File churn, an error streak, or 3+ interruptions."""
    interruptions = s.meta.interruptions if s.meta else 0
    rework = any(f.kind in ("error_streak", "file_churn") for f in s.loops)
    return interruptions >= STRUGGLE_INTERRUPTIONS or rework


def _api(ctx: RuleContext) -> list[RequestRecord]:
    return [r for r in ctx.records if r.source == "proxy"]


def plural(count: int, noun: str) -> str:
    """'1 session', '3 sessions'; nouns here all take a plain -s."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# -- Claude Code: model choice -----------------------------------------------------


def big_model_light_sessions(ctx: RuleContext) -> list[Insight]:
    """Sessions on Opus or Fable that only read, answered and ran commands."""
    picked = [
        s
        for s in claude_code_sessions(ctx)
        if s.is_light
        and s.main_requests >= LIGHT_SESSION_MIN_REQUESTS
        and s.big_model_main_cost >= MIN_SESSION_COST
    ]
    if not picked:
        return []
    ids = {s.session_id for s in picked}
    what = reprice(
        ctx.records,
        ctx.pricing,
        CODING_ALTERNATIVE,
        lambda r: r.session_id in ids and not r.is_sidechain and is_big_model(r.model),
    )
    return [
        insight(
            ctx,
            id="model.big_model_light_sessions",
            category="model",
            scope="claude_code",
            title="Opus or Fable for sessions that changed no files",
            finding=(
                f"{plural(len(picked), 'session')}"
                f" ran the main conversation on the largest models but only "
                f"read code, answered questions or ran commands (no edits): "
                f"${what.current_cost:,.2f}."
            ),
            recommendation=(
                "For questions, reviews and exploration, try Sonnet (`/model sonnet`) or the "
                "`opusplan` alias, and keep Opus for complex architecture and multi-step "
                "reasoning. The saving shown is the same tokens priced on Sonnet 5.5."
            ),
            source=DOCS["costs"],
            sessions=picked,
            savings=what.savings,
            kind="reprice",
        )
    ]


def big_model_subagents(ctx: RuleContext) -> list[Insight]:
    """Subagents that read and searched on the largest models."""
    picked = [s for s in claude_code_sessions(ctx) if s.big_model_subagent_cost >= MIN_SESSION_COST]
    if not picked:
        return []
    ids = {s.session_id for s in picked}
    what = reprice(
        ctx.records,
        ctx.pricing,
        LIGHT_ALTERNATIVE,
        lambda r: r.session_id in ids and r.is_sidechain and is_big_model(r.model),
    )
    return [
        insight(
            ctx,
            id="model.big_model_subagents",
            category="model",
            scope="claude_code",
            title="Subagents run on the largest model",
            finding=(
                f"Subagents in {plural(len(picked), 'session')} ran on Opus or Fable "
                f"(${what.current_cost:,.2f}). Subagents inherit the session's model unless "
                "their definition sets one."
            ),
            recommendation=(
                "Set `model: haiku` (simple searches, log reading) or `model: sonnet` in the "
                "subagent definition. The saving shown is the same tokens priced on Haiku 5.5, "
                "an upper bound for tasks that need more than Haiku."
            ),
            source=DOCS["sub_agents"],
            sessions=picked,
            savings=what.savings,
            kind="upper_bound",
        )
    ]


# -- Claude Code: effort -------------------------------------------------------------


def high_effort_light_work(ctx: RuleContext) -> list[Insight]:
    picked = []
    thinking_cost = 0.0
    for s in claude_code_sessions(ctx):
        high = sum(n for e, n in s.efforts.items() if e in HIGH_EFFORTS)
        share = s.thinking_share
        if (
            s.is_light
            and high > len(s.requests) / 2
            and share is not None
            and share >= THINKING_HEAVY_SHARE
        ):
            picked.append(s)
            thinking_cost += _thinking_cost(s.requests, ctx.pricing)
    if not picked or thinking_cost <= 0:
        return []
    return [
        insight(
            ctx,
            id="effort.high_on_light_work",
            category="effort",
            scope="claude_code",
            title="High effort on sessions that changed no files",
            finding=(
                f"{plural(len(picked), 'session')}"
                f" without edits ran mostly at high/xhigh/max effort, and "
                f"thinking was at least {THINKING_HEAVY_SHARE:.0%} of their output "
                f"(${thinking_cost:,.2f} of thinking)."
            ),
            recommendation=(
                "Use `low` for quick exchanges and `medium` for day-to-day work with a clear "
                "scope (`/effort`); keep `high` for work where edge cases and verification "
                "matter. On Opus 5.5, `medium` matches Opus 5 at `high`. The saving assumes a "
                f"lower effort cuts thinking by {EFFORT_THINKING_REDUCTION:.0%}."
            ),
            source=DOCS["model_config"],
            sessions=picked,
            savings=thinking_cost * EFFORT_THINKING_REDUCTION,
            kind="estimate",
            confidence="low",
        )
    ]


def low_effort_struggle(ctx: RuleContext) -> list[Insight]:
    picked = [
        s
        for s in claude_code_sessions(ctx)
        if s.efforts
        and s.efforts.most_common(1)[0][0] == "low"
        and (any(f.kind in ("error_streak", "file_churn") for f in s.loops))
    ]
    if not picked:
        return []
    return [
        insight(
            ctx,
            id="effort.low_with_struggle",
            category="effort",
            scope="claude_code",
            title="Low effort in sessions that struggled",
            finding=(
                f"{plural(len(picked), 'session')}"
                f" ran mostly at `low` effort and show error streaks or a "
                "file edited over and over."
            ),
            recommendation=(
                "Raise effort to `medium` or `high` for multi-step changes: retries and rework "
                "usually cost more than the thinking a higher effort adds."
            ),
            source=DOCS["model_config"],
            sessions=picked,
            confidence="low",
            level="medium",
        )
    ]


# -- Claude Code: context ------------------------------------------------------------


def kitchen_sink_sessions(ctx: RuleContext) -> list[Insight]:
    picked = [s for s in claude_code_sessions(ctx) if is_kitchen_sink(s)]
    if not picked:
        return []
    carried = sum(context_carry_cost(s.requests, ctx.pricing) for s in picked)
    return [
        insight(
            ctx,
            id="context.kitchen_sink_sessions",
            category="context",
            scope="claude_code",
            title="Long sessions that kept growing",
            finding=(
                f"{plural(len(picked), 'session')}"
                f" took {KITCHEN_SINK_PROMPTS}+ prompts and grew past "
                f"{KITCHEN_SINK_PEAK_TOKENS // 1000}k tokens of context without a reset. Every "
                f"request re-read that history: ${carried:,.2f} went to context above "
                f"{CONTEXT_BASELINE_TOKENS // 1000}k tokens."
            ),
            recommendation=(
                "Run `/clear` between unrelated tasks (`/rename` first to `/resume` later), "
                "`/compact <focus>` when one task runs long, and `/btw` for side questions. "
                "After two failed corrections, start fresh with a better prompt."
            ),
            source=DOCS["best_practices"],
            sessions=picked,
            savings=carried,
            kind="upper_bound",
        )
    ]


def near_context_limit(ctx: RuleContext) -> list[Insight]:
    picked = [
        s
        for s in claude_code_sessions(ctx)
        if s.peak_context_share is not None and s.peak_context_share >= NEAR_LIMIT_SHARE
    ]
    if not picked:
        return []
    return [
        insight(
            ctx,
            id="context.near_limit",
            category="context",
            scope="claude_code",
            title="Sessions close to the context window",
            finding=(
                f"{plural(len(picked), 'session')}"
                f" filled {NEAR_LIMIT_SHARE:.0%}+ of the model's context "
                "window. Quality drops as context fills: earlier instructions get lost."
            ),
            recommendation=(
                "Split the work: delegate investigation to subagents, `/compact` with a focus, "
                "or write a spec and continue in a fresh session."
            ),
            source=DOCS["best_practices"],
            sessions=picked,
            level="medium",
        )
    ]


# -- Claude Code: workflow -----------------------------------------------------------


def exploration_without_subagents(ctx: RuleContext) -> list[Insight]:
    picked = [
        s
        for s in claude_code_sessions(ctx)
        if s.main_reads >= EXPLORATION_MIN_READS and not s.uses_subagents
    ]
    if not picked:
        return []
    return [
        insight(
            ctx,
            id="workflow.exploration_in_main_context",
            category="workflow",
            scope="claude_code",
            title="Investigation filled the main conversation",
            finding=(
                f"{plural(len(picked), 'session')}"
                f" read or searched {EXPLORATION_MIN_READS}+ times in the "
                "main conversation and never used a subagent. Every file read stays in context "
                "for the rest of the session."
            ),
            recommendation=(
                'Ask for "use subagents to investigate X": they explore in their own context and '
                "return a summary. Verbose work (test runs, logs, docs) belongs there too."
            ),
            source=DOCS["best_practices"],
            sessions=picked,
            level="medium",
        )
    ]


def large_changes_without_plan(ctx: RuleContext) -> list[Insight]:
    picked = [s for s in claude_code_sessions(ctx) if is_large_change(s) and not s.used_plan_mode]
    if not picked:
        return []
    return [
        insight(
            ctx,
            id="workflow.no_plan_for_large_changes",
            category="workflow",
            scope="claude_code",
            title="Multi-file changes without plan mode",
            finding=(
                f"{plural(len(picked), 'session')}"
                f" changed {PLAN_MODE_MIN_FILES}+ files without plan mode."
            ),
            recommendation=(
                "For changes across several files, or when the approach is unclear, explore and "
                "plan first (Shift+Tab to plan mode), then implement. Skip it when the diff fits "
                "in one sentence."
            ),
            source=DOCS["best_practices"],
            sessions=picked,
            level="low",
        )
    ]


def struggling_sessions(ctx: RuleContext) -> list[Insight]:
    picked = [s for s in claude_code_sessions(ctx) if has_rework(s)]
    if not picked:
        return []
    churn = sum(1 for s in picked for f in s.loops if f.kind == "file_churn")
    streaks = sum(1 for s in picked for f in s.loops if f.kind == "error_streak")
    return [
        insight(
            ctx,
            id="workflow.rework",
            category="workflow",
            scope="claude_code",
            title="Rework: repeated edits, error streaks, interruptions",
            finding=(
                f"{plural(len(picked), 'session')}"
                f" show rework: {plural(churn, 'file')} edited 5+ times, "
                f"{plural(streaks, 'error streak')}, or 3+ interruptions."
            ),
            recommendation=(
                "Give Claude a check it can run (tests, a build, a screenshot) and say what "
                "done looks like in the prompt. Course-correct early with Esc, rewind with "
                "Esc Esc, and after two failed corrections `/clear` and restate the task."
            ),
            source=DOCS["best_practices"],
            sessions=picked,
            level="medium",
        )
    ]


def tool_denials(ctx: RuleContext) -> list[Insight]:
    picked = [s for s in claude_code_sessions(ctx) if s.meta and s.meta.tool_denials]
    denials = sum(s.meta.tool_denials for s in picked if s.meta)
    manual = [
        s
        for s in claude_code_sessions(ctx)
        if s.meta and s.meta.permission_modes and set(s.meta.permission_modes) <= {"default"}
    ]
    if denials < 3 and len(manual) < 3:
        return []
    return [
        insight(
            ctx,
            id="setup.permissions",
            category="setup",
            scope="claude_code",
            title="Permission prompts interrupt the work",
            finding=(
                f"{plural(denials, 'tool call')}"
                f" denied, and {plural(len(manual), 'session')} ran fully in "
                "manual approval mode."
            ),
            recommendation=(
                "Pre-approve commands you trust with `/permissions` (allowlists), use "
                "`/sandbox`, or auto mode, so approvals go to what actually needs review."
            ),
            source=DOCS["permissions"],
            sessions=picked or manual,
            level="low",
        )
    ]


def claude_md(ctx: RuleContext) -> list[Insight]:
    if not ctx.check_files:
        return []
    by_project: dict[str, list[SessionStats]] = {}
    for s in claude_code_sessions(ctx):
        by_project.setdefault(s.project, []).append(s)
    missing: list[str] = []
    long: list[str] = []
    for project, sessions in by_project.items():
        root = Path(project)
        if len(sessions) < CLAUDE_MD_MIN_SESSIONS or not is_project_dir(root):
            continue
        files = [p for p in (root / "CLAUDE.md", root / ".claude" / "CLAUDE.md") if p.is_file()]
        if not files:
            missing.append(project)
            continue
        lines = sum(_line_count(p) for p in files)
        if lines > CLAUDE_MD_MAX_LINES:
            long.append(f"{shorten_home(project)}: {lines} lines")
    found = []
    if missing:
        found.append(
            insight(
                ctx,
                id="setup.claude_md_missing",
                category="setup",
                scope="claude_code",
                title="Projects without CLAUDE.md",
                finding=(
                    f"No CLAUDE.md in {plural(len(missing), 'project')} with "
                    f"{CLAUDE_MD_MIN_SESSIONS}+ sessions, so each session rediscovers commands "
                    "and conventions."
                ),
                recommendation=(
                    "Run `/init` in the project and keep what Claude can't infer from the code: "
                    "commands, style rules that differ from defaults, test instructions, gotchas."
                ),
                source=DOCS["memory"],
                evidence=[shorten_home(p) for p in missing],
                level="medium",
                confidence="high",
            )
        )
    if long:
        found.append(
            insight(
                ctx,
                id="setup.claude_md_too_long",
                category="setup",
                scope="claude_code",
                title=f"CLAUDE.md longer than {CLAUDE_MD_MAX_LINES} lines",
                finding=(
                    f"CLAUDE.md files in {plural(len(long), 'project')} put more than "
                    f"{CLAUDE_MD_MAX_LINES} lines into every request; long files get rules "
                    "ignored."
                ),
                recommendation=(
                    "Prune it: keep what applies to every session, move workflows that are only "
                    "sometimes relevant into skills (loaded on demand), and turn must-always "
                    "rules into hooks."
                ),
                source=DOCS["costs"],
                evidence=long,
                level="medium",
                confidence="high",
            )
        )
    return found


# -- Prompt cache (both scopes) --------------------------------------------------------


def cache_misses(ctx: RuleContext) -> list[Insight]:
    report = ctx.cache
    avoidable = report.avoidable_penalty
    if avoidable < 0.01:
        return []
    by_id = {s.session_id: s for s in ctx.sessions}
    picked = [
        by_id[s.session_id]
        for s in report.sessions
        if s.avoidable_penalty > 0 and s.session_id in by_id
    ]
    return [
        insight(
            ctx,
            id="cache.avoidable_misses",
            category="cache",
            scope="all",
            title="Avoidable prompt-cache misses",
            finding=f"Cache misses that a stable prefix or a longer TTL would have prevented "
            f"cost ${avoidable:,.2f}. " + " ".join(report.advice[:2]),
            recommendation=(
                "Run `claude-lens cache-doctor` for the misses one by one. Keep tools, system "
                "prompt and settings stable; for pauses of 5-60 minutes use the 1-hour TTL."
            ),
            source=DOCS["prompt_caching"],
            sessions=picked,
            savings=avoidable,
            kind="reprice",
            confidence="high",
        )
    ]


# -- API applications (proxy records) ----------------------------------------------------


def api_no_caching(ctx: RuleContext) -> list[Insight]:
    by_session: dict[str, list[RequestRecord]] = {}
    for r in _api(ctx):
        f = r.features
        if (
            f is not None
            and not f.cache_control
            and r.usage.cache_read_tokens == 0
            and r.usage.cache_write_tokens == 0
            and r.usage.prompt_tokens >= API_CACHE_MIN_PROMPT
        ):
            by_session.setdefault(r.session_id, []).append(r)
    savings = 0.0
    sessions = []
    for session_id, records in by_session.items():
        if len(records) < 2:
            continue
        records.sort(key=lambda r: r.timestamp)
        price = ctx.pricing.price_for(records[0].model)
        if price is None:
            continue
        # Each request after the first could have read the previous prompt from the cache;
        # the first would have paid the 5-minute write premium.
        reusable = sum(
            min(a.usage.prompt_tokens, b.usage.prompt_tokens) for a, b in pairwise(records)
        )
        premium = records[0].usage.prompt_tokens * (ctx.pricing.cache_write_5m_multiplier - 1)
        savings += (reusable * (price.input - price.cache_read_price) - premium * price.input) / 1e6
        sessions.append(session_id)
    if not sessions or savings <= 0:
        return []
    stats = [s for s in ctx.sessions if s.session_id in set(sessions)]
    return [
        insight(
            ctx,
            id="api.no_prompt_caching",
            category="api",
            scope="api",
            title="Repeated large prompts sent without prompt caching",
            finding=(
                f"{plural(len(sessions), 'conversation')} through the proxy resent prompts of "
                f"{API_CACHE_MIN_PROMPT:,}+ tokens with no `cache_control` and no cache reads."
            ),
            recommendation=(
                "Add top-level `cache_control: {type: ephemeral}` (automatic caching) or a "
                "breakpoint after the stable prefix. Keep tools and system prompt byte-stable. "
                "The saving assumes each request could have read the previous prompt."
            ),
            source=DOCS["prompt_caching"],
            sessions=stats,
            savings=savings,
            kind="estimate",
        )
    ]


def api_small_outputs_on_big_model(ctx: RuleContext) -> list[Insight]:
    def small(r: RequestRecord) -> bool:
        return (
            r.source == "proxy"
            and is_big_model(r.model)
            and r.usage.output_tokens < API_SMALL_OUTPUT
            and (r.features is None or r.features.tool_count == 0)
        )

    picked = [r for r in ctx.records if small(r)]
    if len(picked) < 10:
        return []
    what = reprice(ctx.records, ctx.pricing, LIGHT_ALTERNATIVE, small)
    if what.savings <= 0:
        return []
    return [
        insight(
            ctx,
            id="api.small_outputs_on_big_model",
            category="api",
            scope="api",
            title="Short answers from the largest model",
            finding=(
                f"{plural(len(picked), 'API request')}"
                f" without tools got fewer than {API_SMALL_OUTPUT} "
                f"output tokens from Opus or Fable (${what.current_cost:,.2f}): classification, "
                "extraction or routing-sized work."
            ),
            recommendation=(
                "Evaluate Haiku 5.5 or Sonnet 5.5 at low effort on a sample of these requests "
                "before switching; the saving shown is the same tokens priced on Haiku 5.5."
            ),
            source=DOCS["cost_intelligence"],
            evidence=sorted({normalize_model(r.model) for r in picked}),
            savings=what.savings,
            kind="upper_bound",
            confidence="low",
        )
    ]


def api_high_effort_short(ctx: RuleContext) -> list[Insight]:
    picked = [
        r
        for r in _api(ctx)
        if r.effort in HIGH_EFFORTS
        and r.usage.output_tokens < API_SMALL_OUTPUT * 3
        and r.thinking_tokens is not None
        and r.thinking_tokens >= r.usage.output_tokens * THINKING_HEAVY_SHARE
    ]
    if len(picked) < 10:
        return []
    cost = _thinking_cost(picked, ctx.pricing)
    return [
        insight(
            ctx,
            id="api.high_effort_short_answers",
            category="api",
            scope="api",
            title="High effort for short answers",
            finding=(
                f"{plural(len(picked), 'API request')}"
                f" at high/xhigh/max effort produced short answers "
                f"that were mostly thinking (${cost:,.2f} of thinking)."
            ),
            recommendation=(
                "Set `output_config.effort` per route: `low` for chat, classification and "
                "latency-sensitive calls, `medium` as the cost-saving step down. Measure on a "
                "sample before changing the default."
            ),
            source=DOCS["effort"],
            savings=cost * EFFORT_THINKING_REDUCTION,
            kind="estimate",
            confidence="low",
        )
    ]


def api_non_streaming_large(ctx: RuleContext) -> list[Insight]:
    picked = [
        r
        for r in _api(ctx)
        if r.features is not None
        and not r.features.stream
        and (r.features.max_tokens or 0) > API_LARGE_MAX_TOKENS
    ]
    if not picked:
        return []
    return [
        insight(
            ctx,
            id="api.non_streaming_large_max_tokens",
            category="api",
            scope="api",
            title="Large max_tokens without streaming",
            finding=(
                f"{plural(len(picked), 'request')}"
                f" asked for more than {API_LARGE_MAX_TOKENS:,} output "
                "tokens without streaming; long generations risk HTTP timeouts."
            ),
            recommendation=(
                "Stream requests that can produce long output, and use the SDK's final-message "
                "helper when you need the whole response at once."
            ),
            source=DOCS["streaming"],
            level="low",
            confidence="high",
        )
    ]


def api_batch_candidates(ctx: RuleContext) -> list[Insight]:
    by_day: dict[tuple[str, str], list[RequestRecord]] = {}
    for r in _api(ctx):
        if r.features is not None and not r.features.stream and r.features.tool_count == 0:
            key = (r.project, r.timestamp.astimezone().date().isoformat())
            by_day.setdefault(key, []).append(r)
    picked = [r for rs in by_day.values() if len(rs) >= API_BATCH_MIN_REQUESTS for r in rs]
    if not picked:
        return []
    cost = sum(c.total for r in picked if (c := ctx.pricing.cost(r.model, r.usage, r.speed)))
    return [
        insight(
            ctx,
            id="api.batch_candidates",
            category="api",
            scope="api",
            title="High-volume requests that could run as a batch",
            finding=(
                f"{plural(len(picked), 'non-streaming request')} without tools came in bursts of "
                f"{API_BATCH_MIN_REQUESTS}+ a day per project (${cost:,.2f})."
            ),
            recommendation=(
                "If these results are not needed within seconds (evaluations, backfills, "
                "nightly jobs), send them through the Message Batches API at half price."
            ),
            source=DOCS["batch"],
            evidence=sorted({shorten_home(r.project) for r in picked}),
            savings=cost * API_BATCH_DISCOUNT,
            kind="upper_bound",
            confidence="low",
        )
    ]


# -- outcomes: git commits, a rough proxy (correlation only) --------------------------


def edits_without_commit(ctx: RuleContext) -> list[Insight]:
    """Costly sessions in a git repository that edited files and committed nothing."""
    editing = [s for s in claude_code_sessions(ctx) if s.outcome is not None and s.edits]
    picked = [
        s
        for s in editing
        if s.outcome is not None and s.outcome.commits == 0 and s.cost >= NO_COMMIT_MIN_COST
    ]
    if not picked:
        return []
    spent = sum(s.cost for s in picked)
    level = severity(spent, ctx.total_cost)
    return [
        insight(
            ctx,
            id="workflow.edits_without_commit",
            category="workflow",
            scope="claude_code",
            title="Costly sessions that edited files but made no commit",
            finding=(
                f"{plural(len(picked), 'session')} costing ${spent:,.2f} edited files in a git "
                f"repository but made no commit during the session or the "
                f"{OUTCOME_GRACE_MINUTES} minutes after it ({len(picked)} of "
                f"{len(editing)} editing sessions in git repositories)."
            ),
            recommendation=(
                "Look at these first: an abandoned attempt is spend with nothing kept. If the "
                "task was unclear or too large, start in plan mode with a written spec, work in "
                "small verifiable steps and commit as you go; rewind to a checkpoint instead of "
                "fixing forward. Work committed later, by hand or in another repository shows "
                "up here too: commits are a rough proxy for outcome, not a quality measure."
            ),
            source=DOCS["best_practices"],
            sessions=picked,
            confidence="low",
            # Not a saving; never ranked above medium.
            level="medium" if level == "high" else level,
        )
    ]


def _outcome_comparison(
    ctx: RuleContext, group_by: GroupBy, what: str, category: str, source: str
) -> list[Insight]:
    # outcomes imports this module (is_project_dir), so import it here.
    from claude_lens.outcomes import build_outcome_report

    report = build_outcome_report(claude_code_sessions(ctx), group_by)
    rows = [
        r
        for r in report.rows
        if r.key != "unknown"
        and r.sessions >= OUTCOME_MIN_SESSIONS
        and r.commits >= OUTCOME_MIN_COMMITS
        and r.cost_per_commit is not None
    ]
    if len(rows) < 2:
        return []
    rows.sort(key=lambda r: (r.cost_per_commit or 0.0, r.key))
    cheap, dear = rows[0], rows[-1]
    cheap_cpc, dear_cpc = cheap.cost_per_commit or 0.0, dear.cost_per_commit or 0.0
    if not cheap_cpc or dear_cpc < cheap_cpc * OUTCOME_COST_RATIO:
        return []
    evidence = [
        f"{r.key}: ${r.cost_per_commit or 0:,.2f} per commit, "
        + (f"${r.cost_per_100_lines:,.2f} per 100 lines, " if r.cost_per_100_lines else "")
        + f"{plural(r.sessions, 'session')}, {r.edits_no_commit} edited without a commit"
        for r in rows
    ]
    return [
        insight(
            ctx,
            id=f"{category}.outcome_cost_per_commit",
            category=category,
            scope="claude_code",
            title=f"Cost per commit differs by {what} (correlation, not cause)",
            finding=(
                f"Sessions mostly on {dear.key} cost ${dear_cpc:,.2f} per commit, "
                f"{dear_cpc / cheap_cpc:.1f}x the ${cheap_cpc:,.2f} of {cheap.key} "
                f"({dear.sessions} and {cheap.sessions} sessions in git repositories)."
            ),
            recommendation=(
                f"A correlation only: harder tasks tend to get a bigger {what}, and a commit "
                "says nothing about quality. Use it to choose an experiment: run comparable "
                f"tasks on {cheap.key} for a while, then compare cost per commit and sessions "
                f"with edits but no commit in `claude-lens report --outcomes --by {group_by}`."
            ),
            source=source,
            evidence=evidence,
            confidence="low",
            level="info",
        )
    ]


def outcome_by_model(ctx: RuleContext) -> list[Insight]:
    return _outcome_comparison(ctx, "model", "model", "model", DOCS["model_config"])


def outcome_by_effort(ctx: RuleContext) -> list[Insight]:
    return _outcome_comparison(ctx, "effort", "effort", "effort", DOCS["effort"])


RULES: tuple[Rule, ...] = (
    big_model_light_sessions,
    big_model_subagents,
    high_effort_light_work,
    low_effort_struggle,
    kitchen_sink_sessions,
    near_context_limit,
    exploration_without_subagents,
    large_changes_without_plan,
    struggling_sessions,
    tool_denials,
    claude_md,
    cache_misses,
    api_no_caching,
    api_small_outputs_on_big_model,
    api_high_effort_short,
    api_non_streaming_large,
    api_batch_candidates,
    edits_without_commit,
    outcome_by_model,
    outcome_by_effort,
)


# -- helpers -------------------------------------------------------------------------


def _thinking_cost(records: list[RequestRecord], pricing: Pricing) -> float:
    total = 0.0
    for r in records:
        price = pricing.price_for(r.model)
        if price is None or not r.thinking_tokens:
            continue
        scale = price.fast_multiplier if r.speed == "fast" else 1.0
        total += r.thinking_tokens * price.tier(r.usage.prompt_tokens).output * scale / 1e6
    return total


def context_carry_cost(records: list[RequestRecord], pricing: Pricing) -> float:
    """What the main conversation paid for prompt tokens above the baseline size."""
    total = 0.0
    for r in records:
        if r.is_sidechain:
            continue
        price = pricing.price_for(r.model)
        excess = r.usage.prompt_tokens - CONTEXT_BASELINE_TOKENS
        if price is None or excess <= 0:
            continue
        # Most of a long session's history is read from the cache.
        total += excess * price.tier(r.usage.prompt_tokens).cache_read_price / 1e6
    return total


def is_project_dir(path: Path) -> bool:
    """An existing absolute directory outside the temporary-file locations."""
    text = str(path) + "/"
    return path.is_absolute() and not text.startswith(TEMP_ROOTS) and path.is_dir()


def _line_count(path: Path) -> int:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0
