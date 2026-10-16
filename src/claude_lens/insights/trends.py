"""Trends: the habits the insight rules look at, measured per week and compared between ranges.

Every measure reuses the insights definitions (`rules.py` predicates and thresholds,
`engine.build_profile`), and each period is loaded exactly as `insights --since .. --until ..`
would load it, so a week here shows the same numbers as the insights report for that week.
Weeks are ISO weeks of the local calendar (Monday 00:00 local time to the next Monday).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from claude_lens.insights.engine import build_profile, load_context
from claude_lens.insights.models import (
    Comparison,
    MetricChange,
    MetricInfo,
    PeriodMetrics,
    TrendsReport,
    Verdict,
    WeekMetrics,
)
from claude_lens.insights.rules import (
    CONTEXT_BASELINE_TOKENS,
    KITCHEN_SINK_PEAK_TOKENS,
    KITCHEN_SINK_PROMPTS,
    PLAN_MODE_MIN_FILES,
    STRUGGLE_INTERRUPTIONS,
    RuleContext,
    claude_code_sessions,
    context_carry_cost,
    has_rework,
    is_kitchen_sink,
    is_large_change,
    is_long_session,
)
from claude_lens.models import SessionMeta
from claude_lens.pricing import Pricing
from claude_lens.queries import Filters
from claude_lens.store import Store

DEFAULT_WEEKS = 8
# Changes smaller than this count as "same": one percentage point for shares, and for
# amounts a cent or 2% of the previous value, whichever is larger.
SAME_SHARE = 0.01
SAME_USD = 0.01
SAME_USD_RELATIVE = 0.02

METRICS = [
    MetricInfo(
        id="cost",
        label="API-equivalent cost",
        unit="usd",
        better="lower",
        description="Every recorded request at list prices.",
    ),
    MetricInfo(
        id="context_cost",
        label="Context cost",
        unit="usd",
        better="lower",
        description=(
            f"What Claude Code's main conversation paid for prompt tokens above "
            f"{CONTEXT_BASELINE_TOKENS // 1000}k per request."
        ),
    ),
    MetricInfo(
        id="kitchen_sink_share",
        label="Long sessions without reset",
        unit="share",
        better="lower",
        description=(
            f"Of sessions with {KITCHEN_SINK_PROMPTS}+ prompts, the share that grew past "
            f"{KITCHEN_SINK_PEAK_TOKENS // 1000}k tokens of context without a reset."
        ),
    ),
    MetricInfo(
        id="plan_mode_share",
        label="Large changes planned",
        unit="share",
        better="higher",
        description=(
            f"Of sessions changing {PLAN_MODE_MIN_FILES}+ files, the share using plan mode."
        ),
    ),
    MetricInfo(
        id="rework_share",
        label="Sessions with rework",
        unit="share",
        better="lower",
        description=(
            f"Claude Code sessions with file churn, an error streak or "
            f"{STRUGGLE_INTERRUPTIONS}+ interruptions."
        ),
    ),
    MetricInfo(
        id="cache_hit_ratio",
        label="Cache hit ratio",
        unit="share",
        better="higher",
        description="Prompt tokens read from the cache.",
    ),
    MetricInfo(
        id="avoidable_cache_penalty",
        label="Avoidable cache penalty",
        unit="usd",
        better="lower",
        description="Extra cost of cache misses a stable prefix or a longer TTL would prevent.",
    ),
    MetricInfo(
        id="thinking_share",
        label="Thinking share",
        unit="share",
        better=None,
        description="Output tokens spent on thinking (context, not a goal).",
    ),
]


def period_metrics(ctx: RuleContext, since: datetime, until: datetime) -> PeriodMetrics:
    profile = build_profile(ctx, since, until)
    claude_code = claude_code_sessions(ctx)
    large = [s for s in claude_code if is_large_change(s)]
    return PeriodMetrics(
        since=since,
        until=until,
        sessions=profile.sessions,
        requests=profile.requests,
        cost=profile.cost,
        context_cost=sum(context_carry_cost(s.requests, ctx.pricing) for s in claude_code),
        claude_code_sessions=len(claude_code),
        long_sessions=sum(1 for s in claude_code if is_long_session(s)),
        kitchen_sink_sessions=sum(1 for s in claude_code if is_kitchen_sink(s)),
        large_change_sessions=len(large),
        planned_large_change_sessions=sum(1 for s in large if s.used_plan_mode),
        rework_sessions=sum(1 for s in claude_code if has_rework(s)),
        models=profile.models,
        efforts=profile.efforts,
        cache_hit_ratio=profile.cache_hit_ratio,
        avoidable_cache_penalty=ctx.cache.avoidable_penalty,
        thinking_share=profile.thinking_share,
    )


def measure(
    store: Store,
    pricing: Pricing,
    filters: Filters,
    metas: dict[str, SessionMeta] | None = None,
) -> PeriodMetrics:
    if filters.since is None or filters.until is None:
        raise ValueError("a period needs both ends")
    ctx = load_context(store, pricing, filters, check_files=False, metas=metas)
    return period_metrics(ctx, filters.since, filters.until)


def value(metrics: PeriodMetrics, metric_id: str) -> float | None:
    result = getattr(metrics, metric_id)
    return None if result is None else float(result)


def changes(current: PeriodMetrics, previous: PeriodMetrics | None) -> list[MetricChange]:
    rows = []
    for m in METRICS:
        now = value(current, m.id)
        before = value(previous, m.id) if previous else None
        delta = now - before if now is not None and before is not None else None
        rows.append(
            MetricChange(
                id=m.id,
                label=m.label,
                unit=m.unit,
                better=m.better,
                current=now,
                previous=before,
                delta=delta,
                verdict=verdict(m, delta, before),
            )
        )
    return rows


def verdict(metric: MetricInfo, delta: float | None, previous: float | None) -> Verdict:
    if delta is None or previous is None:
        return "n/a"
    if metric.unit == "share":
        small = abs(delta) < SAME_SHARE
    else:
        small = abs(delta) < max(SAME_USD, abs(previous) * SAME_USD_RELATIVE)
    if small:
        return "same"
    if metric.better is None:
        return "changed"
    improved = delta < 0 if metric.better == "lower" else delta > 0
    return "better" if improved else "worse"


# -- weeks ---------------------------------------------------------------------------


def week_start(day: date) -> date:
    """Monday of the ISO week `day` is in."""
    return day - timedelta(days=day.weekday())


def week_label(monday: date) -> str:
    year, week, _ = monday.isocalendar()
    return f"{year}-W{week:02d}"


def local_midnight(day: date) -> datetime:
    """00:00 local time on `day`, as an aware datetime (DST-correct for that date)."""
    return datetime.combine(day, datetime.min.time()).astimezone()


def trends(
    store: Store,
    pricing: Pricing,
    weeks: int = DEFAULT_WEEKS,
    project: str | None = None,
    until: date | None = None,
    today: date | None = None,
) -> TrendsReport:
    """`weeks` local ISO weeks ending with the week of `until` (default: today), oldest first.

    A week that contains `today` is still running and marked `partial`.
    """
    if weeks < 1:
        raise ValueError("weeks must be at least 1")
    today = today or date.today()
    current = week_start(until or today)
    metas = store.session_meta()
    rows: list[WeekMetrics] = []
    for back in range(weeks - 1, -1, -1):
        monday = current - timedelta(weeks=back)
        start, end = local_midnight(monday), local_midnight(monday + timedelta(weeks=1))
        period = measure(store, pricing, Filters(since=start, until=end, project=project), metas)
        running = monday <= today < monday + timedelta(weeks=1)
        week = {"week": week_label(monday), "partial": running}
        rows.append(WeekMetrics.model_validate({**period.model_dump(), **week}))
    return TrendsReport(
        project=project,
        metrics=METRICS,
        weeks=rows,
        changes=changes(rows[-1], rows[-2] if len(rows) > 1 else None),
    )


# -- compare ---------------------------------------------------------------------------


def previous_range(since: datetime, until: datetime) -> tuple[datetime, datetime]:
    """The range of the same length that ends where this one starts.

    Ranges of whole local days are shifted by calendar days, so a DST change in between does
    not move the boundaries off midnight.
    """
    start, end = since.astimezone(), until.astimezone()
    if start == local_midnight(start.date()) and end == local_midnight(end.date()):
        days = (end.date() - start.date()).days
        return local_midnight(start.date() - timedelta(days=days)), start
    return start - (end - start), start


def compare(store: Store, pricing: Pricing, filters: Filters) -> Comparison:
    """`filters`' range against the same-length range before it; open end = end of today."""
    if filters.since is None:
        raise ValueError("comparing needs a start: pass --days or --since")
    until = filters.until or local_midnight(date.today() + timedelta(days=1))
    before_since, before_until = previous_range(filters.since, until)
    metas = store.session_meta()
    now = measure(
        store, pricing, Filters(since=filters.since, until=until, project=filters.project), metas
    )
    before = measure(
        store,
        pricing,
        Filters(since=before_since, until=before_until, project=filters.project),
        metas,
    )
    return Comparison(current=now, previous=before, changes=changes(now, before))
