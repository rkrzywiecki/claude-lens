"""Group proxied requests into sessions without storing any prompt content.

In order of precedence, a request's session is:

1. the `x-lens-session` header;
2. the session id a client puts in `metadata.user_id` (Claude Code sends its own session id
   there, the same id its transcripts use, so both sources agree);
3. message-prefix continuation: a conversation resends its whole history, so request N+1's
   messages start with request N's. Each request's prefix is hashed message by message (the
   system prompt seeds the chain); a request joins the session of the longest prefix seen
   before, or starts a new one. Only hashes are kept, in a bounded in-memory LRU, so these
   sessions do not survive a proxy restart.

`cache_control` markers are ignored while hashing (clients move them to the newest message on
every turn), and string content equals a single text block.

The project is the `x-lens-project` header, else the working directory Claude Code states in the
system prompt or in the messages that open the conversation (before the first assistant turn;
recent versions send it in a `system` role message), else the project already seen for the
session (side calls such as title generation carry no working directory).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import OrderedDict
from typing import Any

from pydantic import BaseModel, ConfigDict

from claude_lens.fingerprint import canonical, normalize_message

DEFAULT_PROJECT = "(proxy)"
DEFAULT_CAPACITY = 4096

# Claude Code states its working directory in the system prompt or an opening message.
_CWD = re.compile(
    r"^\s*(?:-\s*)?(?:Primary working directory|Working directory):\s*(/\S.*?)\s*$", re.M
)
# Older Claude Code versions: user_<hash>_account_<uuid>_session_<uuid>.
_LEGACY_SESSION = re.compile(r"_session_([0-9a-fA-F-]{36})$")


class SessionInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    project: str


class SessionTracker:
    """Not thread-safe: used only from the recorder thread."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity
        self._prefixes: OrderedDict[bytes, SessionInfo] = OrderedDict()
        self._projects: OrderedDict[str, str] = OrderedDict()

    def __len__(self) -> int:
        return len(self._prefixes)

    def assign(
        self, request: Any, session_header: str | None = None, project_header: str | None = None
    ) -> SessionInfo:
        """Session and project of a request body (a parsed JSON object, or anything else)."""
        body = request if isinstance(request, dict) else {}
        hashes = prefix_hashes(body)
        found = next((self._prefixes[h] for h in reversed(hashes) if h in self._prefixes), None)
        session_id = (
            session_header
            or client_session_id(body)
            or (found.session_id if found else f"proxy-{uuid.uuid4()}")
        )
        project = (
            project_header
            or working_directory(body)
            or self._projects.get(session_id)
            or (found.project if found else None)
            or DEFAULT_PROJECT
        )
        info = SessionInfo(session_id=session_id, project=project)
        if hashes:
            _remember(self._prefixes, hashes[-1], info, self._capacity)
        if project != DEFAULT_PROJECT:
            _remember(self._projects, session_id, project, self._capacity)
        return info


def _remember[K, V](cache: OrderedDict[K, V], key: K, value: V, capacity: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > capacity:
        cache.popitem(last=False)


def prefix_hashes(body: dict[str, Any]) -> list[bytes]:
    """Hash of every message prefix: element i covers the system prompt and messages[: i + 1]."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return []
    digest = hashlib.sha256(b"system\0" + canonical(body.get("system"))).digest()
    hashes: list[bytes] = []
    for message in messages:
        digest = hashlib.sha256(digest + canonical(normalize_message(message))).digest()
        hashes.append(digest)
    return hashes


def client_session_id(body: dict[str, Any]) -> str | None:
    """Session id from `metadata.user_id`: Claude Code's JSON form or its older string form."""
    metadata = body.get("metadata")
    user_id = metadata.get("user_id") if isinstance(metadata, dict) else None
    if not isinstance(user_id, str):
        return None
    try:
        parsed = json.loads(user_id)
    except ValueError:
        match = _LEGACY_SESSION.search(user_id)
        return match.group(1) if match else None
    session = parsed.get("session_id") if isinstance(parsed, dict) else None
    return session if isinstance(session, str) and session else None


def working_directory(body: dict[str, Any]) -> str | None:
    """Working directory stated in the system prompt or the messages before the first reply."""
    texts = _texts(body.get("system"))
    messages = body.get("messages")
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get("role") == "assistant":
            break
        texts.extend(_texts(message.get("content")))
    for text in texts:
        match = _CWD.search(text)
        if match:
            return match.group(1)
    return None


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [
            block["text"]
            for block in value
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
    return []
