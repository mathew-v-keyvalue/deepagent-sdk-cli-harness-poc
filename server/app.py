"""FastAPI chat server wrapping harness.agent as a streaming, session-aware
service — the DeepAgents counterpart to the sibling Claude SDK POC's
`server/app.py`.

Like harness/agent.py, this module knows nothing about cybersierra. It only
adapts harness.agent's own small event vocabulary (TextDelta, ToolUseStarted,
Done, Failed) into Server-Sent Events; the security boundary (shell sandbox,
per-call token scoping) lives entirely in harness/agent.py's `_build_agent`,
reused unchanged for every call this server makes.

The SSE contract below (event names and payload shapes, and the two
remaining error statuses) matches the Claude SDK POC's server/app.py — one
deliberate, disclosed exception: `access_token` is no longer required
(`400 no_token` was dropped). See README "Authentication model" for why —
short version: the real `cybersierra` CLI already holds its own auth in a
persisted profile (`~/.cybersierra/config.json`, from a one-time
`cybersierra auth login-browser`), so this POC's "assume already
authenticated" scope means there is no per-request credential to enforce.
`frontend/index.html`'s token field became optional (a small, documented
edit — no longer the byte-for-byte-unmodified copy it started as; see
README for exactly what changed and why).

POC scope: single process, one asyncio event loop, in-process session store
(server/sessions.py) — see that module and README.md for the tradeoffs this
accepts deliberately rather than solving.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from harness.agent import Done, Failed, TextDelta, ToolUseStarted, stream
from harness.observability import configure_logging
from server.sessions import store

load_dotenv(override=False)
configure_logging()  # see harness/observability.py — this is what makes
# cli_call_*/tool_call_*/skill_loaded/turn_* log lines actually print when
# running `uvicorn server.app:app`, not just when embedded in a script
# that configures its own logging.

app = FastAPI(title="cybersierra chat server (DeepAgents POC)")

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/chat")
async def chat(
    message: str = Form(...),
    session_id: str | None = Form(None),
    # Optional, not required — see module docstring. Defaults to "" rather
    # than None so downstream code (harness.agent.stream/run,
    # AllowlistedShellBackend's redact=) always has a plain str to work
    # with; harness.agent treats an empty access_token as "no per-request
    # identity," not as an error.
    access_token: str = Form(""),
):
    is_new_session = session_id is None
    if is_new_session:
        session_id = store.create()
    else:
        entry = store.get(session_id)
        if entry is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "unknown_session",
                        "message": f"no session with id {session_id!r} on this server",
                    }
                },
            )
        # Non-blocking check-then-acquire: safe under asyncio's single-
        # threaded cooperative scheduling because nothing awaits between the
        # check and the acquire call below — only valid as long as this
        # server runs one worker/process (true for this POC's `uvicorn
        # server.app:app`, not automatically true with --workers > 1). Same
        # caveat as the Claude POC's identical check.
        if entry.lock.locked():
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "session_busy",
                        "message": "a turn is already in flight for this session",
                    }
                },
            )

    async def event_source() -> AsyncIterator[str]:
        entry = store.get(session_id)
        assert entry is not None  # just created or looked up above
        await entry.lock.acquire()
        try:
            if is_new_session:
                yield _sse("session", {"session_id": session_id})
                harness_events = stream(message, access_token, session_id=session_id)
            else:
                harness_events = stream(message, access_token, resume=session_id)

            async for event in harness_events:
                if isinstance(event, TextDelta):
                    yield _sse("delta", {"text": event.text})
                elif isinstance(event, ToolUseStarted):
                    yield _sse("tool_use", {"name": event.name})
                elif isinstance(event, Done):
                    store.touch(session_id)
                    yield _sse(
                        "done",
                        {
                            "session_id": event.session_id,
                            "subtype": event.subtype,
                            "total_cost_usd": event.total_cost_usd,
                            "usage": event.usage,
                            "num_turns": event.num_turns,
                        },
                    )
                elif isinstance(event, Failed):
                    if event.code == "session_expired":
                        store.drop(session_id)
                    yield _sse("error", {"code": event.code, "message": event.message})
        finally:
            entry.lock.release()

    return StreamingResponse(event_source(), media_type="text/event-stream")


# Mounted last, at the root, so it never shadows /health or /chat above —
# Starlette tries routes in registration order and this Mount only ever
# catches what nothing more specific already matched.
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
