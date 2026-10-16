"""Incremental reading of one Claude Code session for the live commands.

The status line runs after every assistant message and the prompt hook before every prompt, so
neither can re-read a long transcript each time. What was read is kept in a small state file
per transcript (under LENS_STATE_DIR): the byte offset reached, running totals, and the most
recent responses still open to merging. The next run reads only the bytes appended since.

Parsing is the importer's (`importers/records.py`): one record per message id, never one per
transcript line. Subagent transcripts (`<session>/subagents/*.jsonl`) add to the cost; the
context size and cache state come from the main conversation only.

Nothing here touches the database, and no prompt content is kept: only counts and usage.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from claude_lens.importers.records import is_human_prompt, merge_records, parse_record
from claude_lens.models import RequestRecord
from claude_lens.pricing import Pricing, parse_pricing, pricing_text

STATE_VERSION = 1
# Responses kept open to merging; older ones are folded into the totals. Claude Code writes the
# content blocks of one response back to back, so a handful is plenty.
OPEN_MESSAGES = 16
# State files untouched for this long belong to finished sessions and are removed.
STATE_MAX_AGE_SECONDS = 14 * 24 * 3600
# The first line (up to this many bytes) is hashed to notice a transcript rewritten in place.
HEAD_BYTES = 4096
FIVE_MINUTES = 300
ONE_HOUR = 3600


def state_dir() -> Path:
    """Where live state is kept. Read from the process environment only (no .env file):
    the live commands run inside whatever project Claude Code is working in."""
    return Path(os.environ.get("LENS_STATE_DIR") or "~/.claude-lens/state").expanduser()


def pricing_path() -> Path | None:
    value = os.environ.get("LENS_PRICING_PATH")
    return Path(value).expanduser() if value else None


class FileState(BaseModel):
    """What has been read from one transcript file."""

    inode: int = 0
    offset: int = 0
    # Hash of the file's first bytes: a file rewritten in place starts over.
    head: str = ""
    # Most recent responses, oldest first, still open to merging with later lines.
    open: dict[str, RequestRecord] = Field(default_factory=dict)
    folded_cost: float = 0.0
    folded_requests: int = 0
    folded_unpriced: int = 0
    human_prompts: int = 0
    writes_1h: bool = False
    last_main: RequestRecord | None = None
    peak_context: int = 0


class SessionState(BaseModel):
    version: int = STATE_VERSION
    pricing_id: str = ""
    files: dict[str, FileState] = Field(default_factory=dict)
    # Context window size the status line last saw in Claude Code's own data.
    window_hint: int | None = None


class SessionSnapshot(BaseModel):
    """One session as the live commands see it."""

    model: str | None = None
    # Prompt tokens of the latest main-conversation request: the context as it stands.
    context_tokens: int = 0
    peak_context_tokens: int = 0
    context_window: int | None = None
    last_request_at: datetime | None = None
    # Whether the latest request read or wrote the prompt cache.
    last_request_cached: bool = False
    cache_ttl_seconds: int = FIVE_MINUTES
    cost_usd: float = 0.0
    requests: int = 0
    unpriced_requests: int = 0
    human_prompts: int = 0

    @property
    def context_share(self) -> float | None:
        if not self.context_window or not self.context_tokens:
            return None
        return self.context_tokens / self.context_window

    def cache_seconds_left(self, now: float) -> float | None:
        """Seconds until the cached prefix expires; None when nothing was cached."""
        if self.last_request_at is None or not self.last_request_cached:
            return None
        return self.last_request_at.timestamp() + self.cache_ttl_seconds - now


class LiveSession:
    """Reads a session's transcripts incrementally, keeping state between runs."""

    def __init__(self, transcript: Path, pricing: Pricing, pricing_id: str, root: Path) -> None:
        self.transcript = transcript
        self.pricing = pricing
        self.pricing_id = pricing_id
        self.state_file = root / "sessions" / f"{_key(str(transcript))}.json"
        self.state = self._load()
        self._dirty = False

    @classmethod
    def open(cls, transcript: Path, root: Path | None = None) -> LiveSession:
        text = pricing_text(pricing_path())
        pricing_id = hashlib.sha256(text.encode()).hexdigest()[:16]
        return cls(transcript, parse_pricing(text), pricing_id, root or state_dir())

    def snapshot(self) -> SessionSnapshot:
        """Read what was appended since the last run and summarize the session."""
        main = self._refresh(self.transcript)
        files = [main]
        for path in self._subagent_files():
            files.append(self._refresh(path))

        cost, requests, unpriced = 0.0, 0, 0
        for fs in files:
            cost += fs.folded_cost
            requests += fs.folded_requests + len(fs.open)
            unpriced += fs.folded_unpriced
            for record in fs.open.values():
                priced = self.pricing.cost(record.model, record.usage, record.speed)
                if priced is None:
                    unpriced += 1
                else:
                    cost += priced.total

        last = main.last_main
        window = self.state.window_hint
        if window is None and last is not None:
            window = self.pricing.context_window(last.model)
        return SessionSnapshot(
            model=last.model if last else None,
            context_tokens=last.usage.prompt_tokens if last else 0,
            peak_context_tokens=main.peak_context,
            context_window=window,
            last_request_at=last.timestamp if last else None,
            last_request_cached=bool(
                last and (last.usage.cache_read_tokens or last.usage.cache_write_tokens)
            ),
            cache_ttl_seconds=ONE_HOUR if main.writes_1h else FIVE_MINUTES,
            cost_usd=cost,
            requests=requests,
            unpriced_requests=unpriced,
            human_prompts=main.human_prompts,
        )

    def remember_window(self, window: int | None) -> None:
        if window and window != self.state.window_hint:
            self.state.window_hint = window
            self._dirty = True

    def save(self) -> None:
        """Write the state atomically; a failure only means re-reading next time."""
        if not self._dirty:
            return
        directory = self.state_file.parent
        try:
            is_new = not self.state_file.exists()
            directory.mkdir(parents=True, exist_ok=True)
            tmp = directory / f".{self.state_file.name}.{os.getpid()}.tmp"
            tmp.write_text(self.state.model_dump_json(), encoding="utf-8")
            tmp.replace(self.state_file)
            if is_new:
                prune(directory)
        except OSError:
            return
        self._dirty = False

    def _load(self) -> SessionState:
        try:
            state = SessionState.model_validate_json(self.state_file.read_bytes())
        except (OSError, ValidationError):
            return SessionState(pricing_id=self.pricing_id)
        if state.version != STATE_VERSION or state.pricing_id != self.pricing_id:
            # Folded costs were priced with another table: start over.
            return SessionState(pricing_id=self.pricing_id, window_hint=state.window_hint)
        return state

    def _subagent_files(self) -> list[Path]:
        directory = self.transcript.with_suffix("") / "subagents"
        try:
            return sorted(directory.glob("*.jsonl"))
        except OSError:
            return []

    def _refresh(self, path: Path) -> FileState:
        key = str(path)
        fs = self.state.files.get(key) or FileState()
        try:
            st = path.stat()
        except OSError:
            return fs
        try:
            with path.open("rb") as fh:
                first = fh.read(HEAD_BYTES).split(b"\n", 1)[0]
                head = hashlib.sha256(first).hexdigest()[:16]
                if (
                    st.st_ino != fs.inode
                    or st.st_size < fs.offset
                    or (fs.offset and head != fs.head)
                ):
                    fs = FileState(inode=st.st_ino)
                fs.head = head
                data = b""
                if st.st_size > fs.offset:
                    fh.seek(fs.offset)
                    data = fh.read(st.st_size - fs.offset)
        except OSError:
            return fs
        if data:
            # A line still being written has no newline yet: leave it for the next run.
            end = data.rfind(b"\n") + 1
            for line in data[:end].splitlines():
                self._read_line(fs, line, path)
            fs.offset += end
            self._dirty = self._dirty or end > 0
        self.state.files[key] = fs
        return fs

    def _read_line(self, fs: FileState, line: bytes, path: Path) -> None:
        if not line.strip():
            return
        try:
            entry: Any = json.loads(line)
        except ValueError:
            return
        if not isinstance(entry, dict):
            return
        kind = entry.get("type")
        if kind == "user":
            if is_human_prompt(entry):
                fs.human_prompts += 1
            return
        record = parse_record(entry, path.parent.name)
        if record is None:
            return
        previous = fs.open.pop(record.message_id, None)
        if previous is not None:
            record = merge_records(previous, record)
        fs.open[record.message_id] = record
        if record.usage.cache_write_1h_tokens:
            fs.writes_1h = True
        if not record.is_sidechain:
            fs.peak_context = max(fs.peak_context, record.usage.prompt_tokens)
            last = fs.last_main
            if (
                last is None
                or last.message_id == record.message_id
                or record.timestamp >= (last.timestamp)
            ):
                fs.last_main = record
        while len(fs.open) > OPEN_MESSAGES:
            oldest = next(iter(fs.open))
            folded = fs.open.pop(oldest)
            fs.folded_requests += 1
            priced = self.pricing.cost(folded.model, folded.usage, folded.speed)
            if priced is None:
                fs.folded_unpriced += 1
            else:
                fs.folded_cost += priced.total


def prune(directory: Path, now: float | None = None) -> None:
    """Remove state files of sessions not touched for STATE_MAX_AGE_SECONDS."""
    cutoff = (now if now is not None else time.time()) - STATE_MAX_AGE_SECONDS
    try:
        for path in directory.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        return


def _key(text: str) -> str:
    """File-name-safe key: hashed, so no path or session text reaches the file name."""
    return hashlib.sha256(text.encode()).hexdigest()[:24]
