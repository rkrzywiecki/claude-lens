"""Queries shared by the CLI and the dashboard: store + pricing + filters -> report models."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from pydantic import BaseModel

from claude_lens.analysis.cache import CacheDoctorReport, diagnose
from claude_lens.analysis.loops import LoopFinding, detect_loops
from claude_lens.analysis.session import SessionDetail, session_detail
from claude_lens.models import GroupBy, Report
from claude_lens.pricing import Pricing
from claude_lens.report import build_report
from claude_lens.store import Store


class QueryError(ValueError):
    """Invalid filters, or a session id that matches no session or several."""


class Filters(BaseModel):
    """Time range (`since` inclusive, `until` exclusive) and project substring."""

    since: datetime | None = None
    until: datetime | None = None
    project: str | None = None

    @classmethod
    def from_days(
        cls,
        days: int | None = None,
        since: date | None = None,
        until: date | None = None,
        project: str | None = None,
    ) -> Filters:
        """Local calendar days: the last `days` days including today, or `since`..`until`."""
        if days is not None and since is not None:
            raise QueryError("use either days or since, not both")
        if days is not None and days < 1:
            raise QueryError("days must be at least 1")
        start = _local_midnight(since) if since else None
        if days is not None:
            start = _local_midnight(date.today() - timedelta(days=days - 1))
        end = _local_midnight(until + timedelta(days=1)) if until else None
        return cls(since=start, until=end, project=project or None)


class LoopReport(BaseModel):
    findings: list[LoopFinding]
    # Session id -> project, for display.
    projects: dict[str, str]


def report(
    store: Store, pricing: Pricing, group_by: GroupBy, filters: Filters, top: int | None = None
) -> Report:
    records = store.requests(filters.since, filters.until, filters.project)
    return build_report(records, pricing, group_by, top)


def cache_doctor(
    store: Store, pricing: Pricing, filters: Filters, top: int | None = None
) -> CacheDoctorReport:
    return diagnose(store.requests(filters.since, filters.until, filters.project), pricing, top)


def loops(store: Store, filters: Filters) -> LoopReport:
    findings = detect_loops(store.tool_calls(filters.since, filters.until, project=filters.project))
    projects: dict[str, str] = {}
    for session_id in {f.session_id for f in findings}:
        record = next(store.requests(session=session_id), None)
        if record is not None:
            projects[session_id] = record.project
    return LoopReport(findings=findings, projects=projects)


def resolve_session(store: Store, prefix: str) -> str:
    """Full session id for an id or unique prefix of one."""
    if not prefix:
        raise QueryError("session id is empty")
    matches = store.session_ids(prefix)
    if not matches:
        raise QueryError(f"no session starts with {prefix!r}")
    if prefix in matches:
        return prefix
    if len(matches) > 1:
        shown = ", ".join(m[:12] for m in matches[:5])
        raise QueryError(f"{prefix!r} matches {len(matches)} sessions ({shown}); type more")
    return matches[0]


def session(store: Store, pricing: Pricing, prefix: str) -> SessionDetail:
    session_id = resolve_session(store, prefix)
    records = list(store.requests(session=session_id))
    calls = list(store.tool_calls(session=session_id))
    return session_detail(records, calls, pricing)


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time()).astimezone()
