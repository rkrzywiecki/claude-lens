"""Local dashboard: one HTML page plus a small JSON API over the same queries as the CLI.

The dashboard has no authentication, so it is meant for the loopback interface only, and
requests are accepted only with a loopback `Host` header (a page on another site cannot read the
data through DNS rebinding). POST endpoints also require an `x-lens-request: 1` header, so a
page on another site cannot trigger them. Only `POST /api/coach` calls the Claude API, and only
when the user asks for it.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from claude_lens import queries
from claude_lens.analysis.cache import CacheDoctorReport
from claude_lens.analysis.session import SessionDetail
from claude_lens.config import Settings
from claude_lens.importers.claude_code import import_transcripts
from claude_lens.insights.coach import CoachError, CoachResult, run_coach
from claude_lens.insights.engine import build_insights
from claude_lens.insights.models import InsightsReport, TrendsReport
from claude_lens.insights.trends import DEFAULT_WEEKS, trends
from claude_lens.models import ImportStats, OutcomeReport, Report
from claude_lens.outcomes import Git, refresh_outcomes
from claude_lens.pricing import Pricing, load_pricing
from claude_lens.queries import Filters, LoopReport, QueryError
from claude_lens.report import shorten_home
from claude_lens.store import Store

if TYPE_CHECKING:
    import anthropic

LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "[::1]", "::1"]
TOP_SESSIONS = 10
# Required on POST: a cross-site page cannot add a custom header without a CORS preflight,
# which this server never approves, so it cannot trigger an import or a paid coach call.
ACTION_HEADER = "x-lens-request"


def require_action_header(
    x_lens_request: Annotated[str | None, Header(alias=ACTION_HEADER)] = None,
) -> None:
    if x_lens_request != "1":
        raise HTTPException(status_code=403, detail=f"missing {ACTION_HEADER} header")


class Summary(BaseModel):
    """Everything the overview shows, for one range and project filter."""

    by_day: Report
    by_model: Report
    by_project: Report
    cache: CacheDoctorReport
    loops: LoopReport
    projects: list[str]
    # Git outcome of the sessions in range, by main model (commits: a rough proxy).
    outcomes: OutcomeReport


def create_dashboard(
    settings: Settings,
    open_store: Callable[[], Store] | None = None,
    pricing: Pricing | None = None,
    extra_hosts: list[str] | None = None,
    coach_client: anthropic.Anthropic | None = None,
) -> FastAPI:
    open_store = open_store or (lambda: Store(settings.resolved_db_path()))
    pricing = pricing or load_pricing(settings.resolved_pricing_path())
    app = FastAPI(title="claude-lens", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOOPBACK_HOSTS + (extra_hosts or []))
    page = resources.files("claude_lens").joinpath("web/index.html").read_text("utf-8")

    def filters(days: int | None, project: str | None) -> Filters:
        # Projects are listed with ~ for the home directory; the store holds full paths.
        if project and (project == "~" or project.startswith("~/")):
            project = str(Path.home()) + project[1:]
        try:
            return Filters.from_days(days=days, project=project)
        except QueryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return page

    @app.get("/api/summary")
    def summary(
        days: Annotated[int | None, Query(ge=1)] = None,
        project: str | None = None,
    ) -> Summary:
        selected = filters(days, project)
        with open_store() as store:
            all_projects = queries.report(store, pricing, "project", Filters())
            cache = queries.cache_doctor(store, pricing, selected, TOP_SESSIONS)
            loops = queries.loops(store, selected)
            return Summary(
                by_day=queries.report(store, pricing, "day", selected),
                by_model=queries.report(store, pricing, "model", selected),
                by_project=queries.report(store, pricing, "project", selected),
                cache=_short_cache_paths(cache),
                loops=_short_loop_paths(loops),
                projects=sorted(row.key for row in all_projects.rows),
                outcomes=queries.outcome_report(store, pricing, "model", selected),
            )

    @app.get("/api/insights")
    def insights(
        days: Annotated[int | None, Query(ge=1)] = None,
        project: str | None = None,
    ) -> InsightsReport:
        with open_store() as store:
            return _short_insight_paths(build_insights(store, pricing, filters(days, project)))

    @app.get("/api/trends")
    def weekly_trends(
        weeks: Annotated[int, Query(ge=1, le=104)] = DEFAULT_WEEKS,
        project: str | None = None,
    ) -> TrendsReport:
        selected = filters(None, project)
        with open_store() as store:
            report = trends(store, pricing, weeks, selected.project)
        return report.model_copy(
            update={"project": shorten_home(report.project) if report.project else None}
        )

    @app.post("/api/coach", dependencies=[Depends(require_action_header)])
    def coach(
        days: Annotated[int | None, Query(ge=1)] = None,
        project: str | None = None,
        lang: str | None = None,
    ) -> CoachResult:
        with open_store() as store:
            report = build_insights(store, pricing, filters(days, project))
        try:
            return run_coach(
                report,
                pricing,
                settings.coach_model,
                settings.coach_effort,
                lang or settings.coach_language,
                client=coach_client,
                timeout=settings.coach_timeout,
                backend=settings.coach_backend,
                claude_bin=settings.coach_claude_bin,
            )
        except CoachError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}")
    def session(session_id: str) -> SessionDetail:
        with open_store() as store:
            try:
                detail = queries.session(store, pricing, session_id)
            except QueryError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        outcome = detail.outcome
        if outcome is not None:
            outcome = outcome.model_copy(update={"repo": shorten_home(outcome.repo)})
        return detail.model_copy(
            update={"project": shorten_home(detail.project), "outcome": outcome}
        )

    @app.post("/api/import", dependencies=[Depends(require_action_header)])
    def refresh() -> ImportStats:
        root = settings.resolved_transcripts_dir()
        if not root.is_dir():
            return ImportStats()
        with open_store() as store:
            stats = import_transcripts(store, root)
            if settings.git:
                refresh_outcomes(store, Git(timeout=settings.git_timeout))
            return stats

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _short_cache_paths(report: CacheDoctorReport) -> CacheDoctorReport:
    sessions = [s.model_copy(update={"project": shorten_home(s.project)}) for s in report.sessions]
    return report.model_copy(update={"sessions": sessions})


def _short_loop_paths(report: LoopReport) -> LoopReport:
    findings = [
        f.model_copy(update={"target": shorten_home(f.target) if f.target else None})
        for f in report.findings
    ]
    projects = {k: shorten_home(v) for k, v in report.projects.items()}
    return LoopReport(findings=findings, projects=projects)


def _short_insight_paths(report: InsightsReport) -> InsightsReport:
    home = str(Path.home())
    insights = [
        i.model_copy(update={"evidence": [e.replace(home, "~") for e in i.evidence]})
        for i in report.insights
    ]
    return report.model_copy(update={"insights": insights})
