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
Claude SDK's `resume` does — see below for how that observation has
changed since this was first written).

Behavior change: a caller-supplied session_id unknown to this store no
longer 404s as `unknown_session` — `server/app.py`'s `/chat` handler now
calls `create(session_id)` below and proceeds as a fresh session instead,
so a frontend-minted id can be used starting from message #1 rather than
only a server-minted one. Deliberate tradeoff, not free: this collapses
two previously-distinguishable cases into one. "Genuinely new, frontend-
minted id, never sent before" (the case this exists for) and "a real prior
session whose id this store has since forgotten — e.g. a server restart
wiped this dict" (previously a loud 404) are now indistinguishable from
this store's point of view; the latter now silently starts a brand-new
empty conversation instead of erroring, mirroring exactly the
checkpointer's own long-documented "unseen thread_id just starts fresh,
no error" behavior referenced above. `404 unknown_session` is no longer
reachable through any code path in `server/app.py`.
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

    def create(self, session_id: str | None = None) -> str:
        """Start tracking a session, minting a new id unless the caller
        supplies one — needed so a client-chosen id (e.g. one the frontend
        minted before the first request) can be used from turn one instead
        of only ids this store generates itself."""
        session_id = session_id or str(uuid4())
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
