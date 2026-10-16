"""Git outcomes: log parsing, session windows, cache, migration, metrics, rules and CLI."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import assistant, tool_use, write_jsonl
from typer.testing import CliRunner

from claude_lens.analysis.cache import diagnose
from claude_lens.cli import app
from claude_lens.insights import rules
from claude_lens.insights.coach import coach_payload
from claude_lens.insights.engine import build_insights
from claude_lens.insights.rules import RuleContext
from claude_lens.insights.stats import SessionStats
from claude_lens.models import RequestRecord, SessionOutcome, Usage
from claude_lens.outcomes import (
    Commit,
    Git,
    build_outcome_report,
    parse_log,
    refresh_outcomes,
    resolve_project,
)
from claude_lens.pricing import load_pricing
from claude_lens.queries import Filters
from claude_lens.store import SCHEMA_VERSION, Store

T0 = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
EMAIL = "dev@example.com"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _tmp_is_a_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest's tmp_path lives under a temporary root, which outcomes skip by design."""
    monkeypatch.setattr(rules, "TEMP_ROOTS", ())


def git(repo: Path, *args: str, at: datetime | None = None, email: str = EMAIL) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_COMMITTER_EMAIL": email,
    }
    if at is not None:
        stamp = at.strftime("%Y-%m-%dT%H:%M:%S+0000")
        env |= {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    done = subprocess.run(  # noqa: S603 - fixed test argv
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return done.stdout


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", EMAIL)
    git(path, "config", "commit.gpgsign", "false")
    return path


def commit(
    repo: Path, at: datetime, name: str = "a.txt", lines: int = 3, email: str = EMAIL
) -> None:
    target = repo / name
    old = target.read_text() if target.exists() else ""
    target.write_text(old + "".join(f"line {at:%H%M} {i}\n" for i in range(lines)))
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", "secret commit message", at=at, email=email)


def session_file(tmp_path: Path, session: str, cwd: Path, minutes: list[int], **kw: str) -> None:
    """A transcript whose requests run at T0 + minutes; the first one edits a file."""
    entries = [
        assistant(
            f"{session}-m{i}",
            session=session,
            cwd=str(cwd),
            ts=(T0 + timedelta(minutes=m)).isoformat().replace("+00:00", "Z"),
            content=[tool_use("Edit")] if i == 0 else None,
            output_tokens=200_000,
            **kw,
        )
        for i, m in enumerate(minutes)
    ]
    write_jsonl(tmp_path / "projects" / "p" / f"{session}.jsonl", entries)


class CountingGit(Git):
    def __init__(self) -> None:
        super().__init__()
        self.logs = 0

    def commits(self, top: Path, since: datetime, until: datetime) -> list[Commit]:
        self.logs += 1
        return super().commits(top, since, until)


def windows_store(store: Store, rows: list[tuple[str, Path, int, int]]) -> None:
    """Sessions (id, project, first minute, last minute) as plain request records."""
    records = [
        RequestRecord(
            message_id=f"{sid}-{m}",
            session_id=sid,
            project=str(project),
            model="claude-opus-5-5",
            timestamp=T0 + timedelta(minutes=m),
            usage=Usage(output_tokens=100),
        )
        for sid, project, start, end in rows
        for m in (start, end)
    ]
    store.upsert(records)


# -- parsing ---------------------------------------------------------------------------


def test_parse_log_numstat_with_binary_and_empty_commit() -> None:
    out = (
        "\x00abc123\x1f1760263200\x1fDev@Example.com\n\n"
        "3\t1\tsrc/app.py\n-\t-\tlogo.png\n10\t0\tdocs/a b.md\n"
        "\x00def456\x1f1760263300\x1fdev@example.com\n"
    )
    first, second = parse_log(out)
    assert first.sha == "abc123"
    assert first.committed == datetime.fromtimestamp(1760263200, UTC)
    assert first.committer_email == "dev@example.com"
    assert first.files == [(3, 1, "src/app.py"), (0, 0, "logo.png"), (10, 0, "docs/a b.md")]
    assert second.files == []


def test_parse_log_ignores_garbage() -> None:
    assert parse_log("") == []
    assert parse_log("\x00not a header\n1\t2\tx") == []


# -- window matching -------------------------------------------------------------------


def test_commits_in_window_with_grace_branches_and_other_authors(
    store: Store, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / "repo")
    commit(repo, T0 - timedelta(hours=1), lines=50)  # before the session
    commit(repo, T0 + timedelta(minutes=5), lines=4)  # during
    commit(repo, T0 + timedelta(minutes=5, seconds=30), "b.txt", 2, email="other@x.org")
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, T0 + timedelta(minutes=30), "c.txt", 6)  # on another branch, grace period
    git(repo, "checkout", "-q", "main")
    commit(repo, T0 + timedelta(minutes=60, seconds=30), "d.txt", 9)  # after the grace period
    windows_store(store, [("s1", repo, 0, 20)])

    stats = refresh_outcomes(store)

    assert stats.repos == 1 and stats.sessions == 1 and stats.commits == 2
    outcome = store.outcomes()["s1"]
    assert outcome.commits == 2
    assert outcome.files_changed == 2
    assert outcome.lines_added == 10 and outcome.lines_removed == 0
    assert outcome.repo == str(repo.resolve())


def test_overlapping_sessions_count_each_commit_once(store: Store, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    commit(repo, T0 + timedelta(minutes=2))
    commit(repo, T0 + timedelta(minutes=12))
    windows_store(store, [("early", repo, 0, 15), ("late", repo, 10, 20)])
    refresh_outcomes(store)
    got = store.outcomes()
    assert got["early"].commits == 1
    assert got["late"].commits == 1


def test_sessions_outside_git_are_left_out(store: Store, tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    windows_store(store, [("n", notes, 0, 5), ("gone", tmp_path / "missing", 0, 5)])
    stats = refresh_outcomes(store)
    assert stats.sessions_without_git == 2
    assert store.outcomes() == {}


def test_temporary_directories_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rules, "TEMP_ROOTS", ("/tmp/",))  # noqa: S108
    assert resolve_project("/tmp/scratch") is None  # noqa: S108
    assert resolve_project("relative/path") is None


def test_removed_worktree_maps_to_its_repository(store: Store, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    commit(repo, T0 - timedelta(days=1))
    worktree = repo / ".claude" / "worktrees" / "fix"
    git(repo, "worktree", "add", "-q", "-b", "fix", str(worktree))
    commit(worktree, T0 + timedelta(minutes=3), "w.txt", 5)
    git(repo, "worktree", "remove", "--force", str(worktree))
    assert resolve_project(str(worktree)) == repo
    windows_store(store, [("wt", worktree, 0, 10)])
    refresh_outcomes(store)
    assert store.outcomes()["wt"].lines_added == 5


def test_refresh_is_cached_by_refs(store: Store, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    commit(repo, T0 + timedelta(minutes=1))
    windows_store(store, [("s1", repo, 0, 10)])
    counting = CountingGit()
    refresh_outcomes(store, counting)
    assert counting.logs == 1

    again = refresh_outcomes(store, counting)
    assert again.repos_cached == 1 and counting.logs == 1

    commit(repo, T0 + timedelta(minutes=8))  # the refs move: read again
    refresh_outcomes(store, counting)
    assert counting.logs == 2
    assert store.outcomes()["s1"].commits == 2


def test_missing_git_binary_is_reported(store: Store, tmp_path: Path) -> None:
    windows_store(store, [("s1", tmp_path, 0, 1)])
    stats = refresh_outcomes(store, Git(binary="claude-lens-no-such-git"))
    assert stats.git_missing
    assert store.outcomes() == {}


def test_commit_messages_and_paths_are_not_stored(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    commit(repo, T0 + timedelta(minutes=1), "private-name.txt")
    path = tmp_path / "lens.db"
    with Store(path) as store:
        windows_store(store, [("s1", repo, 0, 5)])
        refresh_outcomes(store)
    data = b"".join(f.read_bytes() for f in tmp_path.glob("lens.db*"))
    assert b"secret commit message" not in data
    assert b"private-name" not in data


# -- migration -------------------------------------------------------------------------


def test_migrates_v4_database(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    with Store(path) as store:
        windows_store(store, [("s1", tmp_path, 0, 1)])
    conn = sqlite3.connect(path)
    conn.executescript("DROP TABLE session_outcomes; PRAGMA user_version = 4;")
    conn.close()
    with Store(path) as store:
        assert store.count() == 2
        assert store.outcomes() == {}
        store.upsert_outcomes(
            [SessionOutcome(session_id="s1", repo="/r", commits=1, window_end=T0, refs_hash="x")]
        )
        assert store.outcomes()["s1"].commits == 1
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    conn.close()


# -- metrics and rules -----------------------------------------------------------------


def stats(
    sid: str,
    *,
    cost: float,
    commits: int | None,
    lines: int = 0,
    model: str = "claude-opus-5-5",
    effort: str = "high",
    edits: int = 1,
) -> SessionStats:
    s = SessionStats(
        session_id=sid,
        project="/work/app",
        source="claude_code",
        started=T0,
        ended=T0 + timedelta(minutes=10),
        cost=cost,
        model_costs={model: cost},
    )
    s.efforts[effort] = 3
    s.main_tool_counts["Edit"] = edits
    if commits is not None:
        s.outcome = SessionOutcome(
            session_id=sid,
            repo="/work/app",
            commits=commits,
            files_changed=commits,
            lines_added=lines,
            window_end=s.ended,
            refs_hash="x",
        )
    return s


def ctx(sessions: list[SessionStats]) -> RuleContext:
    return RuleContext(
        sessions=sessions,
        records=[],
        cache=diagnose([], load_pricing()),
        pricing=load_pricing(),
        total_cost=sum(s.cost for s in sessions),
    )


def test_outcome_report_metrics_by_model() -> None:
    sessions = [
        stats("a", cost=6.0, commits=3, lines=300),
        stats("b", cost=2.0, commits=0),
        stats("c", cost=1.0, commits=1, lines=50, model="claude-sonnet-5-5"),
        stats("d", cost=4.0, commits=None),  # outside git
        stats("e", cost=1.0, commits=0, edits=0),  # read-only, no commit expected
    ]
    report = build_outcome_report(sessions, "model")
    opus, sonnet = report.rows
    assert opus.key == "claude-opus-5-5"
    assert opus.sessions == 3 and opus.commits == 3 and opus.edits_no_commit == 1
    assert opus.cost_per_commit == pytest.approx(3.0)
    assert opus.cost_per_100_lines == pytest.approx(3.0)
    assert sonnet.cost_per_commit == pytest.approx(1.0)
    assert report.untracked_sessions == 1 and report.untracked_cost == pytest.approx(4.0)
    assert report.total.cost_per_commit == pytest.approx(10.0 / 4)
    assert build_outcome_report(sessions, "effort").rows[0].key == "high"


def test_edits_without_commit_rule() -> None:
    sessions = [
        stats("costly", cost=5.0, commits=0),
        stats("cheap", cost=0.5, commits=0),
        stats("ok", cost=9.0, commits=2),
        stats("nogit", cost=9.0, commits=None),
    ]
    [found] = rules.edits_without_commit(ctx(sessions))
    assert found.sessions == ["costly"]
    assert found.confidence == "low"
    assert found.severity in ("medium", "low")
    assert "rough proxy" in found.recommendation


def test_outcome_comparison_needs_samples_and_a_gap() -> None:
    opus = [stats(f"o{i}", cost=4.0, commits=1, lines=40) for i in range(5)]
    sonnet = [stats(f"s{i}", cost=1.0, commits=1, model="claude-sonnet-5-5") for i in range(5)]
    [found] = rules.outcome_by_model(ctx(opus + sonnet))
    assert found.severity == "info" and found.confidence == "low"
    assert "correlation" in found.title
    assert "4.0x" in found.finding
    assert rules.outcome_by_model(ctx(opus + sonnet[:4])) == []  # too few sonnet sessions
    similar = [stats(f"t{i}", cost=3.0, commits=1, model="claude-sonnet-5-5") for i in range(5)]
    assert rules.outcome_by_model(ctx(opus + similar)) == []  # gap below the ratio


# -- end to end ------------------------------------------------------------------------


def two_sessions(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    commit(repo, T0 + timedelta(minutes=4), lines=20)
    session_file(tmp_path, "opus-sess", repo, [0, 5])
    notes = tmp_path / "notes"
    notes.mkdir()
    session_file(tmp_path, "notes-sess", notes, [0, 5])
    return repo


def test_insights_profile_rules_and_coach_payload(store: Store, tmp_path: Path) -> None:
    from claude_lens.importers.claude_code import import_transcripts

    repo = two_sessions(tmp_path)
    import_transcripts(store, tmp_path / "projects")
    refresh_outcomes(store)
    report = build_insights(store, load_pricing(), Filters(), check_files=False)
    assert report.profile.outcomes is not None
    assert report.profile.outcomes.total.commits == 1
    assert report.profile.outcomes.untracked_sessions == 1
    payload = json.dumps(coach_payload(report))
    assert '"git_outcomes"' in payload
    assert str(repo) not in payload and "opus-sess" not in payload


def test_report_outcomes_cli(tmp_path: Path) -> None:
    two_sessions(tmp_path)
    result = runner.invoke(app, ["report", "--outcomes", "--by", "model", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    [row] = data["rows"]
    assert row["key"] == "claude-opus-5-5"
    assert row["commits"] == 1 and row["lines_added"] == 20
    assert row["cost_per_commit"] == pytest.approx(data["total"]["cost"])
    assert data["untracked_sessions"] == 1

    text = runner.invoke(app, ["report", "--outcomes", "--by", "effort", "--no-refresh"])
    assert text.exit_code == 0, text.output
    assert "Cost and git outcome by effort" in text.output
    assert "rough proxy" in text.output


def test_no_git_reads_nothing(tmp_path: Path) -> None:
    two_sessions(tmp_path)
    result = runner.invoke(app, ["report", "--outcomes", "--no-git", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["rows"] == [] and data["untracked_sessions"] == 2


def test_session_cli_shows_outcome(tmp_path: Path) -> None:
    two_sessions(tmp_path)
    result = runner.invoke(app, ["session", "opus"])
    assert result.exit_code == 0, result.output
    assert "Git: 1 commits, 1 files, +20 / -0 lines" in result.output
    data = json.loads(runner.invoke(app, ["session", "notes", "--json"]).output)
    assert data["outcome"] is None


def test_report_by_effort_without_outcomes(tmp_path: Path) -> None:
    two_sessions(tmp_path)
    result = runner.invoke(app, ["report", "--by", "effort", "--json"])
    assert result.exit_code == 0, result.output
    assert [r["key"] for r in json.loads(result.output)["rows"]] == ["unknown"]


def test_dashboard_import_reads_git_and_shows_outcomes(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from claude_lens.config import Settings
    from claude_lens.web.app import create_dashboard

    two_sessions(tmp_path)
    db = tmp_path / "lens.db"
    settings = Settings(db_path=db, transcripts_dir=tmp_path / "projects")
    client = TestClient(
        create_dashboard(settings, open_store=lambda: Store(db)), base_url="http://127.0.0.1"
    )
    assert client.post("/api/import", headers={"x-lens-request": "1"}).status_code == 200
    summary = client.get("/api/summary").json()
    assert summary["outcomes"]["total"]["commits"] == 1
    assert summary["outcomes"]["untracked_sessions"] == 1
    detail = client.get("/api/sessions/opus-sess").json()
    assert detail["outcome"]["lines_added"] == 20
    assert client.get("/api/sessions/notes-sess").json()["outcome"] is None
    page = client.get("/").text
    assert 'id="k-commit"' in page and "s.outcome" in page


def test_dashboard_without_git(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from claude_lens.config import Settings
    from claude_lens.web.app import create_dashboard

    two_sessions(tmp_path)
    db = tmp_path / "lens.db"
    settings = Settings(db_path=db, transcripts_dir=tmp_path / "projects", git=False)
    client = TestClient(
        create_dashboard(settings, open_store=lambda: Store(db)), base_url="http://127.0.0.1"
    )
    client.post("/api/import", headers={"x-lens-request": "1"})
    assert client.get("/api/summary").json()["outcomes"]["total"]["sessions"] == 0
