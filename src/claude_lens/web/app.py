"""Local dashboard: one HTML page plus a small JSON API over the same queries as the CLI.

The dashboard has no authentication, so it is meant for the loopback interface only, and
requests are accepted only with a loopback `Host` header (a page on another site cannot read the
data through DNS rebinding). It never calls the Claude API.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from claude_lens import queries
from claude_lens.analysis.cache import CacheDoctorReport
from claude_lens.analysis.session import SessionDetail
from claude_lens.config import Settings
from claude_lens.importers.claude_code import import_transcripts
from claude_lens.models import ImportStats, Report
from claude_lens.pricing import Pricing, load_pricing
from claude_lens.queries import Filters, LoopReport, QueryError
from claude_lens.report import shorten_home
from claude_lens.store import Store

LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "[::1]", "::1"]
TOP_SESSIONS = 10


class Summary(BaseModel):
    """Everything the overview shows, for one range and project filter."""

    by_day: Report
    by_model: Report
    by_project: Report
    cache: CacheDoctorReport
    loops: LoopReport
    projects: list[str]


def create_dashboard(
    settings: Settings,
    open_store: Callable[[], Store] | None = None,
    pricing: Pricing | None = None,
    extra_hosts: list[str] | None = None,
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
            )

    @app.get("/api/sessions/{session_id}")
    def session(session_id: str) -> SessionDetail:
        with open_store() as store:
            try:
                detail = queries.session(store, pricing, session_id)
            except QueryError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return detail.model_copy(update={"project": shorten_home(detail.project)})

    @app.post("/api/import")
    def refresh() -> ImportStats:
        root = settings.resolved_transcripts_dir()
        if not root.is_dir():
            return ImportStats()
        with open_store() as store:
            return import_transcripts(store, root)

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
