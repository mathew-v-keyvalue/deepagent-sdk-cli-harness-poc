"""FastAPI chat server wrapping harness.agent as a streaming, session-aware
service — the DeepAgents counterpart to the sibling Claude SDK POC's
`server/app.py`.

Like harness/agent.py, this module knows nothing about cybersierra. It only
adapts harness.agent's own small event vocabulary (TextDelta, ToolUseStarted,
Done, Failed) into Server-Sent Events; the security boundary (shell sandbox,
per-call token scoping) lives entirely in harness/agent.py's `_build_agent`,
reused unchanged for every call this server makes.

The SSE contract below (event names and payload shapes) matches the Claude
SDK POC's server/app.py — one deliberate, disclosed exception: `access_token`
is no longer required (`400 no_token` was dropped). See README
"Authentication model" for why — short version: the real `cybersierra` CLI
already holds its own auth in a persisted profile (`~/.cybersierra/
config.json`, from a one-time `cybersierra auth login-browser`), so this
POC's "assume already authenticated" scope means there is no per-request
credential to enforce, unless CYBERSIERRA_INJECT_ACCESS_TOKEN opts into one
(see .env.example and harness/agent.py's `_build_agent`). `frontend/
index.html`'s token field became optional (a small, documented edit — no
longer the byte-for-byte-unmodified copy it started as; see README for
exactly what changed and why).

Two auth layers, at two different boundaries, deliberately not conflated:
`_verify_service_auth` (this module) gates *who may call this server at
all* (a shared secret only morpheus_backend should know); `access_token` +
`CYBERSIERRA_INJECT_ACCESS_TOKEN` (harness/agent.py) is a separate,
per-request concern — *which cybersierra identity a call's CLI subprocess
runs as*. A caller can pass service auth and still send no/garbage
access_token (falls back to the persisted CLI profile); it cannot skip
service auth by presenting a valid access_token.

Only one error status remains from the original two-status contract:
`409 session_busy`. `404 unknown_session` is no longer reachable — an
unrecognized session_id is now treated as that session's first turn
rather than an error; see server/sessions.py's module docstring for the
tradeoff this accepts.

POC scope: single process, one asyncio event loop, in-process session store
(server/sessions.py) — see that module and README.md for the tradeoffs this
accepts deliberately rather than solving.
"""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from harness.agent import Done, Failed, TextDelta, ToolUseStarted, stream
from harness.observability import configure_logging
from harness.tracing import init_tracing
from server.sessions import store

load_dotenv(override=False)
configure_logging()  # see harness/observability.py — this is what makes
# cli_call_*/tool_call_*/skill_loaded/turn_* log lines actually print when
# running `uvicorn server.app:app`, not just when embedded in a script
# that configures its own logging.
init_tracing()  # see harness/tracing.py — no-op unless NETRA_TRACING and
# NETRA_API_KEY are both set; enables the CLI_Call/Plan_Step/Agent_Turn spans.

app = FastAPI(title="cybersierra chat server (DeepAgents POC)")

# Any direct browser caller needs CORS to reach /chat and /health from JS
# `fetch`. As of the morpheus_backend integration, morpheus_fe's "AI Chat"
# widget no longer calls this server directly (it goes through
# morpheus_backend's /tracy/chat proxy instead — same-origin from
# morpheus_fe's point of view, governed by morpheus_backend's own CORS, not
# this one). This now matters only for the bundled `frontend/index.html`
# test UI (already same-origin, unaffected either way) and ad hoc local
# browser testing. FRONTEND_ORIGINS is a comma-separated allowlist.
_allowed_origins = [
    o.strip() for o in os.environ.get("FRONTEND_ORIGINS", "").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins or ["http://localhost:8000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _verify_service_auth(x_service_auth: str | None = Header(None)) -> None:
    """Service-to-service auth for `POST /chat` only — `GET /health` and the
    bundled `frontend/index.html` stay open (health checks and local manual
    testing don't need it).

    A single shared secret (`DEEPAGENT_SERVICE_AUTH`), compared in constant
    time against an `X-Service-Auth` header — not HTTP Basic-Auth, since
    there's no separate username/password here, just one secret both this
    process and its one legitimate caller (morpheus_backend) know.

    Fails closed: an unset `DEEPAGENT_SERVICE_AUTH` refuses every caller
    rather than silently accepting none. Before this, `/chat` had *no*
    service-level auth at all — CORS only stops browser JS, not a direct or
    server-to-server call — meaning anyone who could reach this port could
    execute real CLI commands. That's fine for a single developer's own
    machine, but not once this is reachable from another service.
    """
    expected = os.environ.get("DEEPAGENT_SERVICE_AUTH", "")
    if not expected or not hmac.compare_digest(x_service_auth or "", expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "missing or invalid X-Service-Auth header"},
        )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request, exc: HTTPException) -> JSONResponse:
    # Keeps every error response on this server's one shape
    # (`{"error": {"code": ..., "message": ...}}`), matching the 404/409
    # JSONResponses below rather than FastAPI's default `{"detail": ...}`.
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/chat", dependencies=[Depends(_verify_service_auth)])
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
            # A caller-chosen session_id we've never seen before is treated
            # as that session's first turn, not a 404 — this is what lets a
            # frontend-minted UUID be used starting from message #1 instead
            # of only a server-minted one (see server/sessions.py's
            # create()). Everything else about a brand-new session is
            # unchanged from the session_id-omitted path below.
            store.create(session_id)
            is_new_session = True
        # Non-blocking check-then-acquire: safe under asyncio's single-
        # threaded cooperative scheduling because nothing awaits between the
        # check and the acquire call below — only valid as long as this
        # server runs one worker/process (true for this POC's `uvicorn
        # server.app:app`, not automatically true with --workers > 1). Same
        # caveat as the Claude POC's identical check.
        elif entry.lock.locked():
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
                    yield _sse("tool_use", {"name": event.name, "args": event.args or {}})
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
