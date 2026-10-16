"""Build the insights report: profile, rule results and the practice scorecard."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime

from claude_lens.analysis.cache import diagnose
from claude_lens.analysis.loops import detect_loops
from claude_lens.insights.models import Insight, InsightsReport, Mix, OutcomeSummary, Profile
from claude_lens.insights.practices import practices
from claude_lens.insights.rules import RULES, RuleContext
from claude_lens.insights.stats import SessionStats, build_session_stats
from claude_lens.models import RequestRecord, SessionMeta
from claude_lens.outcomes import build_outcome_report
from claude_lens.pricing import Pricing, normalize_model
from claude_lens.queries import Filters
from claude_lens.store import Store

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def load_context(
    store: Store,
    pricing: Pricing,
    filters: Filters,
    check_files: bool = True,
    metas: dict[str, SessionMeta] | None = None,
) -> RuleContext:
    """Records, tool calls and per-session statistics of one range: what the rules look at."""
    records = list(store.requests(filters.since, filters.until, filters.project))
    calls = list(store.tool_calls(filters.since, filters.until, project=filters.project))
    loops = detect_loops(calls)
    metas = store.session_meta() if metas is None else metas
    sessions = build_session_stats(records, calls, metas, loops, pricing, store.outcomes())
    return RuleContext(
        sessions=sessions,
        records=records,
        cache=diagnose(records, pricing),
        pricing=pricing,
        total_cost=sum(s.cost for s in sessions),
        check_files=check_files,
    )


def build_insights(
    store: Store, pricing: Pricing, filters: Filters, check_files: bool = True
) -> InsightsReport:
    ctx = load_context(store, pricing, filters, check_files)
    found: list[Insight] = [i for rule in RULES for i in rule(ctx)]
    found.sort(key=lambda i: (_SEVERITY_ORDER[i.severity], -(i.savings_usd or 0.0), i.id))
    return InsightsReport(
        profile=build_profile(ctx, filters.since, filters.until),
        insights=found,
        practices=practices(ctx),
    )


def build_profile(ctx: RuleContext, since: datetime | None, until: datetime | None) -> Profile:
    return _profile(ctx.records, ctx.sessions, ctx.pricing, since, until)


def _profile(
    records: list[RequestRecord],
    sessions: list[SessionStats],
    pricing: Pricing,
    since: datetime | None,
    until: datetime | None,
) -> Profile:
    by_model: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    by_effort: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    by_entry: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    total = cc_cost = api_cost = sub_cost = 0.0
    output = thinking = read = prompt = 0
    entry_of = {s.session_id: (s.meta.entrypoint if s.meta else None) for s in sessions}
    for r in records:
        c = pricing.cost(r.model, r.usage, r.speed)
        amount = c.total if c else 0.0
        total += amount
        if r.source == "proxy":
            api_cost += amount
        else:
            cc_cost += amount
        if r.is_sidechain:
            sub_cost += amount
        for table, key in (
            (by_model, normalize_model(r.model)),
            (by_effort, r.effort or "unknown"),
            (
                by_entry,
                "api (proxy)" if r.source == "proxy" else entry_of.get(r.session_id) or "unknown",
            ),
        ):
            table[key][0] += 1
            table[key][1] += amount
        output += r.usage.output_tokens
        thinking += r.thinking_tokens or 0
        read += r.usage.cache_read_tokens
        prompt += r.usage.prompt_tokens
    peaks = sorted(s.peak_prompt_tokens for s in sessions)
    return Profile(
        since=since,
        until=until,
        sessions=len(sessions),
        requests=len(records),
        cost=total,
        claude_code_cost=cc_cost,
        api_cost=api_cost,
        models=_mix(by_model, total),
        efforts=_mix(by_effort, total),
        entrypoints=_mix(by_entry, total),
        thinking_share=thinking / output if output and thinking else None,
        subagent_cost_share=sub_cost / total if total else None,
        cache_hit_ratio=read / prompt if prompt else None,
        median_session_requests=(
            statistics.median(len(s.requests) for s in sessions) if sessions else None
        ),
        p95_peak_prompt_tokens=peaks[min(len(peaks) - 1, int(len(peaks) * 0.95))]
        if peaks
        else None,
        outcomes=outcome_summary(sessions),
    )


def outcome_summary(sessions: list[SessionStats]) -> OutcomeSummary | None:
    """Commits per model and effort; None when no session ran in a git repository."""
    by_model = build_outcome_report(sessions, "model")
    if not by_model.total.sessions:
        return None
    return OutcomeSummary(
        total=by_model.total,
        by_model=by_model.rows,
        by_effort=build_outcome_report(sessions, "effort").rows,
        untracked_sessions=by_model.untracked_sessions,
    )


def _mix(table: dict[str, list[float]], total: float) -> list[Mix]:
    rows = [
        Mix(key=k, requests=int(v[0]), cost=v[1], share=v[1] / total if total else 0.0)
        for k, v in table.items()
    ]
    return sorted(rows, key=lambda m: (-m.cost, m.key))
