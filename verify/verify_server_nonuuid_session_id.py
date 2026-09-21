"""morpheus_backend's tracy_session.service.ts now mints session ids as the
stringified auto-increment `id` of a newly created `ai_conversations` row
(e.g. "42"), not a `randomUUID()` — see the Tracy session-durability work in
that repo. This harness never validated session_id's format (confirmed by
reading server/sessions.py/server/app.py/harness/agent.py: it's used purely
as an opaque string -- a dict key here, a `thread_id` for the checkpointer
there), so no harness code change should be needed. This script proves that
directly, rather than trusting the read.

Same two departures from the sibling verify scripts' usual pattern as
verify_server_user_permissions.py, for the same reasons (in-process ASGI
transport so this script's own `server.sessions.store` is the same object
`/chat` mutates; a scripted model since this is pure plumbing, not model
behavior).

Two scenarios:

1. A client-supplied session_id in the new integer-string shape, unknown to
   this (fresh) process, is accepted as a new session rather than rejected
   -- matches server/sessions.py's documented "unknown session_id just
   starts fresh" behavior, exercised here specifically with a non-UUID id
   to prove that behavior doesn't secretly assume UUID shape.
2. A second turn reusing that same integer-string session_id resumes the
   same SessionEntry (not a new one) -- proves it works as a stable
   identifier across turns, not just accepted once.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DEEPAGENT_SERVICE_AUTH", "verify-nonuuid-session-id-test-secret")
TEST_SERVICE_AUTH = os.environ["DEEPAGENT_SERVICE_AUTH"]

from server.app import app  # noqa: E402 — after env var is set, see above
from server.sessions import store  # noqa: E402 — same in-process object /chat mutates

PLACEHOLDER_TOKEN = "placeholder-token-not-real"

# The actual shape morpheus_backend now sends: String(autoIncrementId), not a UUID.
NONUUID_SESSION_ID = "42"


class ScriptedToolCallModel(BaseChatModel):
    """No tool calls needed -- this is pure session-id plumbing, unrelated to
    what the model does. Copied, not imported, from the sibling verify
    scripts: each one here is a standalone diagnostic tool."""

    responses: list[AIMessage] = []
    _i: int = 0

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        msg = self.responses[self._i]
        self._i += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])

    @property
    def _llm_type(self) -> str:
        return "scripted"


def _scripted_model(reply: str) -> ScriptedToolCallModel:
    return ScriptedToolCallModel(responses=[AIMessage(content=reply)])


async def _turn(client: httpx.AsyncClient, session_id: str, reply: str) -> tuple[int, str]:
    form = {"message": "hello", "access_token": PLACEHOLDER_TOKEN, "session_id": session_id}
    with patch("harness.agent.resolve_model", return_value=_scripted_model(reply)):
        resp = await client.post("/chat", data=form)
    return resp.status_code, resp.text


async def main() -> int:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=60.0, headers={"X-Service-Auth": TEST_SERVICE_AUTH}
    ) as client:
        # 1. Backend-style integer-string session_id, never seen by this process -> accepted as a fresh session.
        status, raw = await _turn(client, NONUUID_SESSION_ID, "first reply")
        if status != 200:
            print(f"FAIL: turn 1 (fresh non-UUID session_id) returned {status}: {raw}")
            return 1
        entry = store.get(NONUUID_SESSION_ID)
        if entry is None:
            print(f"FAIL: no SessionEntry found for session_id={NONUUID_SESSION_ID!r} after turn 1")
            return 1
        print(f"  ok: session_id={NONUUID_SESSION_ID!r} (non-UUID shape) accepted as a fresh session")

        # 2. Second turn, same non-UUID session_id -> resumes the SAME SessionEntry.
        entry_before = store.get(NONUUID_SESSION_ID)
        status, raw = await _turn(client, NONUUID_SESSION_ID, "second reply")
        if status != 200:
            print(f"FAIL: turn 2 (resume) returned {status}: {raw}")
            return 1
        entry_after = store.get(NONUUID_SESSION_ID)
        if entry_after is None:
            print("FAIL: SessionEntry disappeared on turn 2")
            return 1
        if entry_after is not entry_before:
            print("FAIL: turn 2 created a distinct SessionEntry instead of resuming the same one")
            return 1
        print(f"  ok: turn 2 resumed the same SessionEntry via session_id={NONUUID_SESSION_ID!r}")

        print("PASS: the harness treats session_id as a fully opaque string -- a non-UUID, integer-string id (matching morpheus_backend's new minting scheme) works identically to the old randomUUID() shape, on both a fresh and a resumed turn.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
