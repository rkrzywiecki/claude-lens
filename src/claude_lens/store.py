"""SQLite store for request records and import bookkeeping."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from claude_lens.models import RequestRecord, ToolCall, Usage

SCHEMA_VERSION = 3

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
    segments        TEXT
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
)

# _MIGRATIONS[n - 1] upgrades a database from version n to n + 1.
_MIGRATIONS = (
    "ALTER TABLE requests ADD COLUMN ttft_ms REAL;ALTER TABLE requests ADD COLUMN latency_ms REAL;",
    # v3 adds tool calls: forget imported files so the next import reads them again.
    # Fixed DDL only, no values.
    "ALTER TABLE requests ADD COLUMN segments TEXT; DELETE FROM imported_files;" + _TOOL_CALLS,  # noqa: S608
)

_COLUMNS = (
    "message_id, request_id, session_id, project, model, ts, input_tokens, output_tokens, "
    "cache_read, cache_write_5m, cache_write_1h, speed, stop_reason, tool_names, is_sidechain, "
    "source, ttft_ms, latency_ms, segments"
)

# The same response can arrive from both sources. Usage and tool names come from the newest
# write; latency is kept once measured; a proxy record never replaces the session, project and
# start time of an imported one (the transcript knows the real session, the proxy only guesses).
_KEEP_IMPORTED = "requests.source = 'claude_code' AND excluded.source = 'proxy'"
# Built only from the constants above; values are bound as parameters.
_UPSERT = f"""
INSERT INTO requests ({_COLUMNS}) VALUES ({", ".join("?" * 19)})
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
    segments = COALESCE(excluded.segments, requests.segments)
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
