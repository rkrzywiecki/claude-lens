"""SQLite store for request records and import bookkeeping."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from claude_lens.models import (
    RequestFeatures,
    RequestRecord,
    SessionMeta,
    SessionOutcome,
    ToolCall,
    Usage,
)

SCHEMA_VERSION = 5

_TOOL_CALLS = """
CREATE TABLE IF NOT EXISTS tool_calls (
    tool_use_id  TEXT PRIMARY KEY,
    message_id   TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    ts           TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    input_hash   TEXT NOT NULL,
    target       TEXT,
    is_error     INTEGER,
    source       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_calls_session ON tool_calls (session_id, ts);
"""

_SESSION_META = """
CREATE TABLE IF NOT EXISTS session_meta (
    session_id        TEXT NOT NULL,
    source_file       TEXT NOT NULL,
    entrypoint        TEXT,
    version           TEXT,
    permission_modes  TEXT NOT NULL,
    human_prompts     INTEGER NOT NULL,
    interruptions     INTEGER NOT NULL,
    tool_denials      INTEGER NOT NULL,
    api_errors        INTEGER NOT NULL,
    skills            TEXT NOT NULL,
    agents            TEXT NOT NULL,
    active_ms         INTEGER NOT NULL,
    PRIMARY KEY (session_id, source_file)
);
"""

# Git outcome per session: counts only, never commit messages, diffs or file names.
_SESSION_OUTCOMES = """
CREATE TABLE IF NOT EXISTS session_outcomes (
    session_id     TEXT PRIMARY KEY,
    repo           TEXT NOT NULL,
    commits        INTEGER NOT NULL,
    files_changed  INTEGER NOT NULL,
    lines_added    INTEGER NOT NULL,
    lines_removed  INTEGER NOT NULL,
    window_end     TEXT NOT NULL,
    refs_hash      TEXT NOT NULL
);
"""

_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS requests (
    message_id      TEXT PRIMARY KEY,
    request_id      TEXT,
    session_id      TEXT NOT NULL,
    project         TEXT NOT NULL,
    model           TEXT NOT NULL,
    ts              TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cache_read      INTEGER NOT NULL,
    cache_write_5m  INTEGER NOT NULL,
    cache_write_1h  INTEGER NOT NULL,
    speed           TEXT NOT NULL,
    stop_reason     TEXT,
    tool_names      TEXT NOT NULL,
    is_sidechain    INTEGER NOT NULL,
    source          TEXT NOT NULL,
    ttft_ms         REAL,
    latency_ms      REAL,
    segments        TEXT,
    effort          TEXT,
    thinking_tokens INTEGER,
    agent_id        TEXT,
    features        TEXT
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests (ts);
CREATE INDEX IF NOT EXISTS requests_session ON requests (session_id);
CREATE TABLE IF NOT EXISTS imported_files (
    path   TEXT PRIMARY KEY,
    size   INTEGER NOT NULL,
    mtime  REAL NOT NULL
);
"""
    + _TOOL_CALLS
    + _SESSION_META
    + _SESSION_OUTCOMES
)

# _MIGRATIONS[n - 1] upgrades a database from version n to n + 1.
_MIGRATIONS = (
    "ALTER TABLE requests ADD COLUMN ttft_ms REAL;ALTER TABLE requests ADD COLUMN latency_ms REAL;",
    # v3 adds tool calls: forget imported files so the next import reads them again.
    # Fixed DDL only, no values.
    "ALTER TABLE requests ADD COLUMN segments TEXT; DELETE FROM imported_files;" + _TOOL_CALLS,  # noqa: S608
    # v4 adds effort, thinking and session metadata: read every transcript again.
    "\n".join(
        [
            "ALTER TABLE requests ADD COLUMN effort TEXT;",
            "ALTER TABLE requests ADD COLUMN thinking_tokens INTEGER;",
            "ALTER TABLE requests ADD COLUMN agent_id TEXT;",
            "ALTER TABLE requests ADD COLUMN features TEXT;",
            "DELETE FROM imported_files;",
            _SESSION_META,
        ]
    ),
    # v5 adds git outcomes per session; filled by `claude-lens outcomes`, nothing to re-import.
    _SESSION_OUTCOMES,
)

_COLUMNS = (
    "message_id, request_id, session_id, project, model, ts, input_tokens, output_tokens, "
    "cache_read, cache_write_5m, cache_write_1h, speed, stop_reason, tool_names, is_sidechain, "
    "source, ttft_ms, latency_ms, segments, effort, thinking_tokens, agent_id, features"
)

# The same response can arrive from both sources. Usage and tool names come from the newest
# write; latency is kept once measured; a proxy record never replaces the session, project and
# start time of an imported one (the transcript knows the real session, the proxy only guesses).
_KEEP_IMPORTED = "requests.source = 'claude_code' AND excluded.source = 'proxy'"
# Built only from the constants above; values are bound as parameters.
_UPSERT = f"""
INSERT INTO requests ({_COLUMNS}) VALUES ({", ".join("?" * 23)})
ON CONFLICT (message_id) DO UPDATE SET
    request_id = COALESCE(excluded.request_id, requests.request_id),
    session_id = CASE WHEN {_KEEP_IMPORTED} THEN requests.session_id ELSE excluded.session_id END,
    project = CASE WHEN {_KEEP_IMPORTED} THEN requests.project ELSE excluded.project END,
    ts = CASE WHEN {_KEEP_IMPORTED} THEN requests.ts ELSE excluded.ts END,
    is_sidechain = CASE WHEN {_KEEP_IMPORTED} THEN requests.is_sidechain
        ELSE excluded.is_sidechain END,
    source = CASE WHEN {_KEEP_IMPORTED} THEN requests.source ELSE excluded.source END,
    model = excluded.model,
    input_tokens = excluded.input_tokens,
    output_tokens = excluded.output_tokens,
    cache_read = excluded.cache_read,
    cache_write_5m = excluded.cache_write_5m,
    cache_write_1h = excluded.cache_write_1h,
    speed = excluded.speed,
    stop_reason = COALESCE(excluded.stop_reason, requests.stop_reason),
    tool_names = excluded.tool_names,
    ttft_ms = COALESCE(excluded.ttft_ms, requests.ttft_ms),
    latency_ms = COALESCE(excluded.latency_ms, requests.latency_ms),
    segments = COALESCE(excluded.segments, requests.segments),
    effort = COALESCE(excluded.effort, requests.effort),
    thinking_tokens = COALESCE(excluded.thinking_tokens, requests.thinking_tokens),
    agent_id = COALESCE(excluded.agent_id, requests.agent_id),
    features = COALESCE(excluded.features, requests.features)
"""  # noqa: S608

_TOOL_COLUMNS = (
    "tool_use_id, message_id, session_id, ts, tool_name, input_hash, target, is_error, source"
)
_KEEP_IMPORTED_CALL = "tool_calls.source = 'claude_code' AND excluded.source = 'proxy'"
_UPSERT_TOOL_CALL = f"""
INSERT INTO tool_calls ({_TOOL_COLUMNS}) VALUES ({", ".join("?" * 9)})
ON CONFLICT (tool_use_id) DO UPDATE SET
    session_id = CASE WHEN {_KEEP_IMPORTED_CALL} THEN tool_calls.session_id
        ELSE excluded.session_id END,
    ts = CASE WHEN {_KEEP_IMPORTED_CALL} THEN tool_calls.ts ELSE excluded.ts END,
    source = CASE WHEN {_KEEP_IMPORTED_CALL} THEN tool_calls.source ELSE excluded.source END,
    input_hash = excluded.input_hash,
    target = COALESCE(excluded.target, tool_calls.target),
    is_error = COALESCE(excluded.is_error, tool_calls.is_error)
"""  # noqa: S608


def _ts(value: datetime) -> str:
    """Fixed-width UTC timestamp, so string order in SQLite equals time order."""
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class StoreError(RuntimeError):
    """The database is unusable (wrong schema version, corrupt file)."""


class Store:
    def __init__(self, path: Path) -> None:
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        (version,) = self._conn.execute("PRAGMA user_version").fetchone()
        if not 0 <= version <= SCHEMA_VERSION:
            raise StoreError(f"database schema version {version}, expected {SCHEMA_VERSION}")
        if version == SCHEMA_VERSION:
            return
        # A new database gets the current schema; an older one each missing migration. Every
        # step bumps user_version in the same transaction, so an interrupted upgrade resumes.
        if version == 0:
            steps = [(_SCHEMA, SCHEMA_VERSION)]
        else:
            steps = [(_MIGRATIONS[v - 1], v + 1) for v in range(version, SCHEMA_VERSION)]
        for script, target in steps:
            self._conn.executescript(f"BEGIN; {script} PRAGMA user_version = {target}; COMMIT;")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- import bookkeeping -------------------------------------------------

    def is_unchanged(self, path: Path, size: int, mtime: float) -> bool:
        row = self._conn.execute(
            "SELECT size, mtime FROM imported_files WHERE path = ?", (str(path),)
        ).fetchone()
        return row is not None and row[0] == size and row[1] == mtime

    def mark_imported(self, path: Path, size: int, mtime: float) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO imported_files (path, size, mtime) VALUES (?, ?, ?)",
                (str(path), size, mtime),
            )

    def forget_imports(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM imported_files")

    # -- requests -------------------------------------------------------------

    def upsert(self, records: Iterable[RequestRecord]) -> int:
        rows = [
            (
                r.message_id,
                r.request_id,
                r.session_id,
                r.project,
                r.model,
                _ts(r.timestamp),
                r.usage.input_tokens,
                r.usage.output_tokens,
                r.usage.cache_read_tokens,
                r.usage.cache_write_5m_tokens,
                r.usage.cache_write_1h_tokens,
                r.speed,
                r.stop_reason,
                json.dumps(r.tool_names),
                int(r.is_sidechain),
                r.source,
                r.ttft_ms,
                r.latency_ms,
                json.dumps(r.segments) if r.segments is not None else None,
                r.effort,
                r.thinking_tokens,
                r.agent_id,
                r.features.model_dump_json() if r.features is not None else None,
            )
            for r in records
        ]
        with self._conn:
            self._conn.executemany(_UPSERT, rows)
        return len(rows)

    def requests(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        project: str | None = None,
        session: str | None = None,
    ) -> Iterator[RequestRecord]:
        """Records ordered by time; `since` inclusive, `until` exclusive (aware datetimes)."""
        clauses, params = _filters(since, until, project, session)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # Clause strings are fixed above; only values come from the caller, as parameters.
        query = f"SELECT {_COLUMNS} FROM requests {where} ORDER BY ts"  # noqa: S608
        for row in self._conn.execute(query, params):
            yield RequestRecord(
                message_id=row[0],
                request_id=row[1],
                session_id=row[2],
                project=row[3],
                model=row[4],
                timestamp=datetime.fromisoformat(row[5]),
                usage=Usage(
                    input_tokens=row[6],
                    output_tokens=row[7],
                    cache_read_tokens=row[8],
                    cache_write_5m_tokens=row[9],
                    cache_write_1h_tokens=row[10],
                ),
                speed=row[11],
                stop_reason=row[12],
                tool_names=json.loads(row[13]),
                is_sidechain=bool(row[14]),
                source=row[15],
                ttft_ms=row[16],
                latency_ms=row[17],
                segments=json.loads(row[18]) if row[18] is not None else None,
                effort=row[19],
                thinking_tokens=row[20],
                agent_id=row[21],
                features=(
                    RequestFeatures.model_validate_json(row[22]) if row[22] is not None else None
                ),
            )

    def count(self) -> int:
        (n,) = self._conn.execute("SELECT COUNT(*) FROM requests").fetchone()
        return int(n)

    def session_ids(self, prefix: str) -> list[str]:
        """Sessions whose id starts with `prefix` (for short ids typed on the command line)."""
        rows = self._conn.execute(
            "SELECT DISTINCT session_id FROM requests WHERE substr(session_id, 1, ?) = ? "
            "ORDER BY session_id",
            (len(prefix), prefix),
        )
        return [row[0] for row in rows]

    # -- session metadata -------------------------------------------------------

    def upsert_session_meta(self, metas: Iterable[SessionMeta]) -> None:
        rows = [
            (
                m.session_id,
                m.source_file,
                m.entrypoint,
                m.version,
                json.dumps(m.permission_modes),
                m.human_prompts,
                m.interruptions,
                m.tool_denials,
                m.api_errors,
                json.dumps(m.skills),
                json.dumps(m.agents),
                m.active_ms,
            )
            for m in metas
        ]
        with self._conn:
            self._conn.executemany(
                f"INSERT OR REPLACE INTO session_meta VALUES ({', '.join('?' * 12)})",  # noqa: S608
                rows,
            )

    def session_meta(self) -> dict[str, SessionMeta]:
        """Metadata per session, summed over the files that mention it."""
        merged: dict[str, SessionMeta] = {}
        for row in self._conn.execute("SELECT * FROM session_meta ORDER BY session_id"):
            meta = SessionMeta(
                session_id=row[0],
                source_file=row[1],
                entrypoint=row[2],
                version=row[3],
                permission_modes=json.loads(row[4]),
                human_prompts=row[5],
                interruptions=row[6],
                tool_denials=row[7],
                api_errors=row[8],
                skills=json.loads(row[9]),
                agents=json.loads(row[10]),
                active_ms=row[11],
            )
            current = merged.get(meta.session_id)
            merged[meta.session_id] = meta if current is None else _merge_meta(current, meta)
        return merged

    # -- session windows and outcomes ------------------------------------------

    def session_windows(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> Iterator[tuple[str, str, datetime, datetime]]:
        """(session id, project, first request, last request) for every session in range."""
        clauses, params = _filters(since, until, None, None)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # The project of a session's first request; clause strings are fixed above.
        query = f"""
            SELECT session_id,
                   (SELECT project FROM requests r2 WHERE r2.session_id = r.session_id
                    ORDER BY ts LIMIT 1),
                   MIN(ts), MAX(ts)
            FROM requests r {where} GROUP BY session_id ORDER BY MIN(ts)
        """  # noqa: S608
        for row in self._conn.execute(query, params):
            yield row[0], row[1], datetime.fromisoformat(row[2]), datetime.fromisoformat(row[3])

    def upsert_outcomes(self, outcomes: Iterable[SessionOutcome]) -> None:
        rows = [
            (
                o.session_id,
                o.repo,
                o.commits,
                o.files_changed,
                o.lines_added,
                o.lines_removed,
                _ts(o.window_end),
                o.refs_hash,
            )
            for o in outcomes
        ]
        with self._conn:
            self._conn.executemany(
                f"INSERT OR REPLACE INTO session_outcomes VALUES ({', '.join('?' * 8)})",  # noqa: S608
                rows,
            )

    def delete_outcomes(self, session_ids: Iterable[str]) -> None:
        with self._conn:
            self._conn.executemany(
                "DELETE FROM session_outcomes WHERE session_id = ?", [(s,) for s in session_ids]
            )

    def outcomes(self) -> dict[str, SessionOutcome]:
        rows = self._conn.execute("SELECT * FROM session_outcomes ORDER BY session_id")
        return {
            row[0]: SessionOutcome(
                session_id=row[0],
                repo=row[1],
                commits=row[2],
                files_changed=row[3],
                lines_added=row[4],
                lines_removed=row[5],
                window_end=datetime.fromisoformat(row[6]),
                refs_hash=row[7],
            )
            for row in rows
        }

    # -- tool calls -----------------------------------------------------------

    def upsert_tool_calls(self, calls: Iterable[ToolCall]) -> int:
        rows = [
            (
                c.tool_use_id,
                c.message_id,
                c.session_id,
                _ts(c.timestamp),
                c.tool_name,
                c.input_hash,
                c.target,
                None if c.is_error is None else int(c.is_error),
                c.source,
            )
            for c in calls
        ]
        with self._conn:
            self._conn.executemany(_UPSERT_TOOL_CALL, rows)
        return len(rows)

    def mark_tool_results(self, results: Iterable[tuple[str, bool]]) -> None:
        """Record whether each tool_use id's result was an error (unknown ids are ignored)."""
        with self._conn:
            self._conn.executemany(
                "UPDATE tool_calls SET is_error = ? WHERE tool_use_id = ?",
                [(int(is_error), tool_use_id) for tool_use_id, is_error in results],
            )

    def tool_calls(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        session: str | None = None,
        project: str | None = None,
    ) -> Iterator[ToolCall]:
        """Tool calls ordered by session and time. `project` matches the calling request's."""
        clauses, params = _filters(since, until, None, session)
        if project is not None:
            clauses.append("message_id IN (SELECT message_id FROM requests WHERE project LIKE ?)")
            params.append(f"%{project}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # Clause strings are fixed; only values come from the caller, as parameters.
        query = f"SELECT {_TOOL_COLUMNS} FROM tool_calls {where} ORDER BY session_id, ts"  # noqa: S608
        for row in self._conn.execute(query, params):
            yield ToolCall(
                tool_use_id=row[0],
                message_id=row[1],
                session_id=row[2],
                timestamp=datetime.fromisoformat(row[3]),
                tool_name=row[4],
                input_hash=row[5],
                target=row[6],
                is_error=None if row[7] is None else bool(row[7]),
                source=row[8],
            )


def _filters(
    since: datetime | None,
    until: datetime | None,
    project: str | None,
    session: str | None,
) -> tuple[list[str], list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if since is not None:
        clauses.append("ts >= ?")
        params.append(_ts(since))
    if until is not None:
        clauses.append("ts < ?")
        params.append(_ts(until))
    if project is not None:
        clauses.append("project LIKE ?")
        params.append(f"%{project}%")
    if session is not None:
        clauses.append("session_id = ?")
        params.append(session)
    return clauses, params


def _merge_meta(a: SessionMeta, b: SessionMeta) -> SessionMeta:
    def union(x: list[str], y: list[str]) -> list[str]:
        return x + [v for v in y if v not in x]

    return SessionMeta(
        session_id=a.session_id,
        source_file="*",
        entrypoint=a.entrypoint or b.entrypoint,
        version=max(filter(None, [a.version, b.version]), default=None),
        permission_modes=union(a.permission_modes, b.permission_modes),
        human_prompts=a.human_prompts + b.human_prompts,
        interruptions=a.interruptions + b.interruptions,
        tool_denials=a.tool_denials + b.tool_denials,
        api_errors=a.api_errors + b.api_errors,
        skills=union(a.skills, b.skills),
        agents=union(a.agents, b.agents),
        active_ms=a.active_ms + b.active_ms,
    )
