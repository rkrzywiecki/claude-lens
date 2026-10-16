"""SQLite store for request records and import bookkeeping."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from claude_lens.models import RequestRecord, Usage

SCHEMA_VERSION = 1

_SCHEMA = """
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
    source          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests (ts);
CREATE INDEX IF NOT EXISTS requests_session ON requests (session_id);
CREATE TABLE IF NOT EXISTS imported_files (
    path   TEXT PRIMARY KEY,
    size   INTEGER NOT NULL,
    mtime  REAL NOT NULL
);
"""


def _ts(value: datetime) -> str:
    """Fixed-width UTC timestamp, so string order in SQLite equals time order."""
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class StoreError(RuntimeError):
    """The database is unusable (wrong schema version, corrupt file)."""


class Store:
    def __init__(self, path: Path) -> None:
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        (version,) = self._conn.execute("PRAGMA user_version").fetchone()
        if version not in (0, SCHEMA_VERSION):
            raise StoreError(f"database schema version {version}, expected {SCHEMA_VERSION}")
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
            )
            for r in records
        ]
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO requests VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def requests(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        project: str | None = None,
    ) -> Iterator[RequestRecord]:
        """Records ordered by time; `since` inclusive, `until` exclusive (aware datetimes)."""
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
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # Clause strings are fixed above; only values come from the caller, as parameters.
        query = f"SELECT * FROM requests {where} ORDER BY ts"  # noqa: S608
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
            )

    def count(self) -> int:
        (n,) = self._conn.execute("SELECT COUNT(*) FROM requests").fetchone()
        return int(n)
