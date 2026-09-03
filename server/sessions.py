"""In-process chat session store.

POC scope: a plain dict, no TTL/eviction, lost on restart, doesn't work
across multiple processes — identical scope to the sibling Claude SDK POC's
`server/sessions.py`. There, that scope is justified because "the SDK's own
on-disk transcript, keyed by the same session_id, is exactly as durable as
this needs to be for a single-process POC." Here the equivalent durability
claim is about `harness.agent._checkpointer` (an `InMemorySaver`, also
process-lifetime) — same shape, see README "Session continuity: checkpointer
vs on-disk transcript" for how that comparison holds and one place it
doesn't (a checkpointer never raises on an unknown thread_id the way the
Claude SDK's `resume` does; this store, not the checkpointer, is what makes
`unknown_session` a real 404 here too).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class SessionEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionEntry] = {}

    def create(self) -> str:
        session_id = str(uuid4())
        self._sessions[session_id] = SessionEntry()
        return session_id

    def get(self, session_id: str) -> SessionEntry | None:
        return self._sessions.get(session_id)

    def touch(self, session_id: str) -> None:
        entry = self._sessions.get(session_id)
        if entry is not None:
            entry.last_active_at = time.monotonic()

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


# One store for the life of the process. A real (non-POC) multi-process
# deployment would need this to live somewhere shared instead.
store = SessionStore()
