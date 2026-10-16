"""Best-practice scorecard: documented practices and whether the activity follows them.

Each check measures one behavior across the sessions in range and says pass, warn or fail with
the numbers behind it, or n/a when the data cannot tell (no such sessions, or no API traffic).
"""

from __future__ import annotations

from pathlib import Path

from claude_lens.insights import rules as r
from claude_lens.insights.models import Practice, PracticeStatus
from claude_lens.insights.stats import SessionStats

WARN_SHARE = 0.10
FAIL_SHARE = 0.30


def _status(bad: int, total: int) -> PracticeStatus:
    if total == 0:
        return "n/a"
    share = bad / total
    if share >= FAIL_SHARE:
        return "fail"
    if share >= WARN_SHARE:
        return "warn"
    return "pass"


def _share(bad: int, total: int, what: str) -> str:
    """'3 of 10 long sessions grew ...' or, with nothing to measure, 'no long sessions in range'.

    `what` starts with the population ("long sessions") followed by the behavior after " | ".
    """
    population, behavior = what.split(" | ")
    return f"{bad} of {total} {population} {behavior}" if total else f"no {population} in range"


def practices(ctx: r.RuleContext) -> list[Practice]:
    cc = [s for s in ctx.sessions if s.source == "claude_code"]
    api = [rec for rec in ctx.records if rec.source == "proxy"]
    substantial = [s for s in cc if s.main_requests >= r.LIGHT_SESSION_MIN_REQUESTS]
    out: list[Practice] = []

    def add(
        id: str,  # noqa: A002
        title: str,
        scope: str,
        bad: int,
        total: int,
        what: str,
        source: str,
    ) -> None:
        out.append(
            Practice.model_validate(
                {
                    "id": id,
                    "title": title,
                    "scope": scope,
                    "status": _status(bad, total),
                    "evidence": _share(bad, total, what),
                    "source": source,
                }
            )
        )

    long_running = [s for s in cc if s.human_prompts >= r.KITCHEN_SINK_PROMPTS]
    add(
        "clear_between_tasks",
        "Clear context between unrelated tasks",
        "claude_code",
        sum(
            1
            for s in long_running
            if s.peak_prompt_tokens >= r.KITCHEN_SINK_PEAK_TOKENS and s.resets == 0
        ),
        len(long_running),
        "long sessions | grew past 150k tokens without a reset",
        r.DOCS["best_practices"],
    )
    light = [s for s in substantial if s.is_light]
    add(
        "match_model_to_task",
        "Match the model to the task",
        "claude_code",
        sum(1 for s in light if s.big_model_main_cost >= r.MIN_SESSION_COST),
        len(light),
        "sessions without edits | ran on Opus or Fable",
        r.DOCS["costs"],
    )
    subagent_sessions = [s for s in cc if s.subagent_requests]
    add(
        "smaller_models_for_subagents",
        "Run simple subagents on smaller models",
        "claude_code",
        sum(1 for s in subagent_sessions if s.big_model_subagent_cost >= r.MIN_SESSION_COST),
        len(subagent_sessions),
        "sessions with subagents | ran them on Opus or Fable",
        r.DOCS["sub_agents"],
    )
    add(
        "tune_effort",
        "Tune effort to the task",
        "claude_code",
        sum(
            1
            for s in light
            if sum(n for e, n in s.efforts.items() if e in r.HIGH_EFFORTS) > len(s.requests) / 2
        ),
        len(light),
        "sessions without edits | ran mostly at high effort or above",
        r.DOCS["model_config"],
    )
    exploring = [s for s in cc if s.main_reads >= r.EXPLORATION_MIN_READS]
    add(
        "subagents_for_investigation",
        "Use subagents for investigation",
        "claude_code",
        sum(1 for s in exploring if not s.uses_subagents),
        len(exploring),
        "read-heavy sessions | never used a subagent",
        r.DOCS["best_practices"],
    )
    large = [s for s in cc if len(s.edited_files) >= r.PLAN_MODE_MIN_FILES]
    add(
        "plan_before_large_changes",
        "Plan before multi-file changes",
        "claude_code",
        sum(1 for s in large if not s.used_plan_mode),
        len(large),
        "sessions that changed 5+ files | did so without plan mode",
        r.DOCS["best_practices"],
    )
    editing = [s for s in cc if s.edits]
    add(
        "verify_and_course_correct",
        "Verify work and course-correct early",
        "claude_code",
        sum(1 for s in editing if _struggled(s)),
        len(editing),
        "sessions with edits | show rework (repeated edits, error streaks, interruptions)",
        r.DOCS["best_practices"],
    )
    add(
        "concise_claude_md",
        "Keep a concise CLAUDE.md",
        "claude_code",
        *_claude_md_counts(ctx),
        r.DOCS["memory"],
    )
    cache_sessions = [s for s in ctx.sessions if s.cache and s.cache.requests > 1]
    add(
        "keep_cache_warm",
        "Keep the prompt cache warm and the prefix stable",
        "all",
        sum(1 for s in cache_sessions if s.cache and s.cache.avoidable_penalty > 0),
        len(cache_sessions),
        "multi-request sessions | had avoidable cache misses",
        r.DOCS["prompt_caching"],
    )
    large_prompts = [rec for rec in api if rec.usage.prompt_tokens >= r.API_CACHE_MIN_PROMPT]
    add(
        "api_prompt_caching",
        "Cache large repeated prompts (API)",
        "api",
        sum(
            1
            for rec in large_prompts
            if rec.features is not None
            and not rec.features.cache_control
            and rec.usage.cache_read_tokens == 0
        ),
        len(large_prompts),
        "large API requests | used no prompt cache",
        r.DOCS["prompt_caching"],
    )
    big_max = [
        rec
        for rec in api
        if rec.features and (rec.features.max_tokens or 0) > r.API_LARGE_MAX_TOKENS
    ]
    add(
        "api_streaming",
        "Stream long generations (API)",
        "api",
        sum(1 for rec in big_max if rec.features and not rec.features.stream),
        len(big_max),
        "API requests with a large max_tokens | did not stream",
        r.DOCS["streaming"],
    )
    return out


def _struggled(s: SessionStats) -> bool:
    interruptions = s.meta.interruptions if s.meta else 0
    return interruptions >= r.STRUGGLE_INTERRUPTIONS or any(
        f.kind in ("error_streak", "file_churn") for f in s.loops
    )


def _claude_md_counts(ctx: r.RuleContext) -> tuple[int, int, str]:
    """(projects with a problem, projects checked, description) from the CLAUDE.md rule."""
    what = "active projects | lack CLAUDE.md or exceed 200 lines"
    if not ctx.check_files:
        return 0, 0, what
    bad = sum(len(i.evidence) for i in r.claude_md(ctx))
    projects = {s.project for s in ctx.sessions if s.source == "claude_code"}
    checked = sum(
        1
        for p in projects
        if sum(1 for s in ctx.sessions if s.project == p) >= r.CLAUDE_MD_MIN_SESSIONS
        and r.is_project_dir(Path(p))
    )
    return bad, checked, what
