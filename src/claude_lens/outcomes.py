"""Git outcomes: commits made in a session's repository while the session ran.

A rough proxy for outcome, not for quality: a commit says that work was kept, not that it was
good, and work committed later (or by hand outside the window) is missed. Sessions whose
project directory is not a local git repository (notes, research, temporary directories) are
left out instead of being counted as "no commit".

Git is run read-only: a fixed argument list, no shell, a timeout, no pager, no external diff,
textconv or signature checks, and optional locks off. Only counts are kept: commit messages,
diffs and file names never leave this module.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from claude_lens.insights.rules import OUTCOME_GRACE_MINUTES, is_project_dir
from claude_lens.insights.stats import SessionStats
from claude_lens.models import GroupBy, OutcomeReport, OutcomeRow, SessionOutcome
from claude_lens.report import shorten_home
from claude_lens.store import Store

# Commits up to this long after a session's last request still count for it.
GRACE = timedelta(minutes=OUTCOME_GRACE_MINUTES)
GIT_TIMEOUT = 10.0
# Claude Code worktrees live here; once removed, the main repository still has their branches.
_WORKTREE_MARKER = "/.claude/worktrees/"
_FIELD = "\x1f"
_RECORD = "\x00"
# No pager, no colors, no prompts, no index refresh; output in a fixed locale.
_GIT_ENV = {
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C",
}


class GitError(RuntimeError):
    """A git command failed or timed out."""


class Commit(BaseModel):
    """One commit as read from `git log --numstat`; lives in memory only."""

    sha: str
    committed: datetime
    committer_email: str
    # (lines added, lines removed, path) per file; binary files count 0 lines.
    files: list[tuple[int, int, str]] = Field(default_factory=list)


class RepoInfo(BaseModel):
    top: Path
    # Shared by every worktree of one repository.
    common_dir: Path


class RefreshStats(BaseModel):
    repos: int = 0
    repos_cached: int = 0
    sessions: int = 0
    sessions_without_git: int = 0
    commits: int = 0
    errors: int = 0
    git_missing: bool = False


class Git:
    """Read-only git runner (fixed argv, no shell, timeout)."""

    def __init__(self, binary: str = "git", timeout: float = GIT_TIMEOUT) -> None:
        self.binary = binary
        self.timeout = timeout

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def run(self, cwd: Path, *args: str) -> str:
        argv = [self.binary, "--no-pager", "-c", "core.fsmonitor=false", "-C", str(cwd), *args]
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
                stdin=subprocess.DEVNULL,
                env={**os.environ, **_GIT_ENV},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"git {args[0]}: {exc}") from exc
        if done.returncode != 0:
            raise GitError(f"git {args[0]} exited with {done.returncode}")
        return done.stdout

    def repo_info(self, path: Path) -> RepoInfo | None:
        try:
            out = self.run(path, "rev-parse", "--show-toplevel", "--git-common-dir")
        except GitError:
            return None
        lines = out.splitlines()
        if len(lines) < 2:
            return None
        top = Path(lines[0])
        common = Path(lines[1])
        if not common.is_absolute():
            common = path / common
        return RepoInfo(top=top, common_dir=common.resolve())

    def refs_hash(self, top: Path) -> str:
        """Changes whenever a branch, tag, HEAD or worktree moves (the cache key)."""
        digest = hashlib.sha256()
        for args in (
            ("for-each-ref", "--format=%(objectname) %(refname)"),
            ("worktree", "list", "--porcelain"),
        ):
            digest.update(self.run(top, *args).encode())
        try:
            digest.update(self.run(top, "rev-parse", "HEAD").encode())
        except GitError:
            digest.update(b"no HEAD")  # a repository without commits
        return digest.hexdigest()

    def user_email(self, top: Path) -> str | None:
        try:
            return self.run(top, "config", "--get", "user.email").strip().lower() or None
        except GitError:
            return None

    def commits(self, top: Path, since: datetime, until: datetime) -> list[Commit]:
        """Non-merge commits on every branch and worktree, committed in [since, until]."""
        out = self.run(
            top,
            "log",
            "--exclude=refs/stash",
            "--all",
            "--no-merges",
            "--no-renames",
            "--numstat",
            "--no-ext-diff",
            "--no-textconv",
            "--no-show-signature",
            "--no-color",
            f"--since={_git_time(since - timedelta(minutes=1))}",
            f"--until={_git_time(until + timedelta(minutes=1))}",
            "--format=%x00%H%x1f%ct%x1f%ce",
        )
        return [c for c in parse_log(out) if since <= c.committed <= until]


def _git_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_log(out: str) -> list[Commit]:
    """Parse `git log --numstat --format=%x00%H%x1f%ct%x1f%ce` output."""
    commits: list[Commit] = []
    for chunk in out.split(_RECORD):
        lines = chunk.strip("\n").split("\n")
        header = lines[0].split(_FIELD)
        if len(header) != 3 or not header[1].isdigit():
            continue
        files: list[tuple[int, int, str]] = []
        for line in lines[1:]:
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, removed, path = parts
            # Binary files show "-" for both counts.
            files.append((_count(added), _count(removed), path))
        commits.append(
            Commit(
                sha=header[0],
                committed=datetime.fromtimestamp(int(header[1]), UTC),
                committer_email=header[2].strip().lower(),
                files=files,
            )
        )
    return commits


def _count(value: str) -> int:
    return int(value) if value.isdigit() else 0


def resolve_project(project: str) -> Path | None:
    """The directory to read git from: the project, or the repository of a removed worktree."""
    path = Path(project)
    if not path.is_absolute():
        return None
    if not path.exists() and _WORKTREE_MARKER in project:
        path = Path(project.split(_WORKTREE_MARKER, 1)[0])
    return path if is_project_dir(path) else None


class _Window(BaseModel):
    session_id: str
    start: datetime
    end: datetime

    @property
    def until(self) -> datetime:
        return self.end + GRACE


def assign_commits(windows: list[_Window], commits: Iterable[Commit]) -> dict[str, list[Commit]]:
    """Each commit goes to one session: the latest one started before it whose window holds it."""
    ordered = sorted(windows, key=lambda w: w.start)
    assigned: dict[str, list[Commit]] = defaultdict(list)
    seen: set[str] = set()
    for commit in commits:
        if commit.sha in seen:
            continue
        seen.add(commit.sha)
        owner = None
        for w in ordered:
            if w.start <= commit.committed <= w.until:
                owner = w
        if owner is not None:
            assigned[owner.session_id].append(commit)
    return assigned


def _clusters(windows: list[_Window]) -> list[tuple[datetime, datetime]]:
    """Merge overlapping windows so each stretch of time is read from git once."""
    spans: list[tuple[datetime, datetime]] = []
    for w in sorted(windows, key=lambda w: w.start):
        if spans and w.start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], w.until))
        else:
            spans.append((w.start, w.until))
    return spans


def summarize(
    session_id: str, repo: Path, commits: list[Commit], window_end: datetime, refs: str
) -> SessionOutcome:
    files = {path for c in commits for _, _, path in c.files}
    return SessionOutcome(
        session_id=session_id,
        repo=str(repo),
        commits=len(commits),
        files_changed=len(files),
        lines_added=sum(a for c in commits for a, _, _ in c.files),
        lines_removed=sum(r for c in commits for _, r, _ in c.files),
        window_end=window_end,
        refs_hash=refs,
    )


def refresh_outcomes(store: Store, git: Git | None = None) -> RefreshStats:
    """Read git for every session whose repository changed since the last run."""
    git = git or Git()
    stats = RefreshStats()
    if not git.available():
        stats.git_missing = True
        return stats
    known = store.outcomes()
    # Sessions per repository (worktrees of one repository are read together).
    by_repo: dict[Path, list[_Window]] = defaultdict(list)
    tops: dict[Path, Path] = {}
    infos: dict[str, RepoInfo | None] = {}
    for session_id, project, start, end in store.session_windows():
        path = resolve_project(project)
        if path is None:
            stats.sessions_without_git += 1
            continue
        key = str(path)
        if key not in infos:
            infos[key] = git.repo_info(path)
        info = infos[key]
        if info is None:
            stats.sessions_without_git += 1
            continue
        by_repo[info.common_dir].append(_Window(session_id=session_id, start=start, end=end))
        tops.setdefault(info.common_dir, info.top)
    for common, windows in by_repo.items():
        stats.repos += 1
        stats.sessions += len(windows)
        top = tops[common]
        try:
            refs = git.refs_hash(top)
            if all(
                (o := known.get(w.session_id)) is not None
                and o.refs_hash == refs
                and o.window_end == w.end
                for w in windows
            ):
                stats.repos_cached += 1
                continue
            email = git.user_email(top)
            commits = [
                c
                for since, until in _clusters(windows)
                for c in git.commits(top, since, until)
                if email is None or c.committer_email == email
            ]
        except GitError:
            stats.errors += 1
            continue
        assigned = assign_commits(windows, commits)
        stats.commits += sum(len(v) for v in assigned.values())
        store.upsert_outcomes(
            summarize(w.session_id, top, assigned.get(w.session_id, []), w.end, refs)
            for w in windows
        )
    return stats


# -- metrics -------------------------------------------------------------------------


def add_session(row: OutcomeRow, stats: SessionStats, outcome: SessionOutcome) -> None:
    row.sessions += 1
    row.cost += stats.cost
    row.commits += outcome.commits
    row.files_changed += outcome.files_changed
    row.lines_added += outcome.lines_added
    row.lines_removed += outcome.lines_removed
    if outcome.commits:
        row.sessions_with_commits += 1
    elif stats.edits:
        row.edits_no_commit += 1


def outcome_key(stats: SessionStats, group_by: GroupBy) -> str:
    match group_by:
        case "day":
            return stats.started.astimezone().date().isoformat()
        case "project":
            return shorten_home(stats.project)
        case "model":
            return stats.main_model or "unknown"
        case "effort":
            return stats.main_effort or "unknown"
        case "session":
            return stats.session_id


def build_outcome_report(
    sessions: Iterable[SessionStats], group_by: GroupBy, top: int | None = None
) -> OutcomeReport:
    """Group sessions that have an outcome; days in time order, other groups by cost."""
    rows: dict[str, OutcomeRow] = {}
    total = OutcomeRow(key="total")
    untracked = 0
    untracked_cost = 0.0
    for s in sessions:
        if s.outcome is None:
            untracked += 1
            untracked_cost += s.cost
            continue
        key = outcome_key(s, group_by)
        row = rows.get(key)
        if row is None:
            label = None
            if group_by == "session":
                label = f"{s.started.astimezone():%Y-%m-%d %H:%M}  {shorten_home(s.project)}"
            row = rows[key] = OutcomeRow(key=key, label=label)
        add_session(row, s, s.outcome)
        add_session(total, s, s.outcome)
    ordered = list(rows.values())
    if group_by == "day":
        ordered.sort(key=lambda r: r.key)
    else:
        ordered.sort(key=lambda r: (-r.cost, r.key))
    if top is not None:
        ordered = ordered[-top:] if group_by == "day" else ordered[:top]
    return OutcomeReport(
        group_by=group_by,
        rows=ordered,
        total=total,
        untracked_sessions=untracked,
        untracked_cost=untracked_cost,
    )
