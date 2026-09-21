"""SessionEntry.user_permissions actually gets populated from the `/chat`
endpoint's `permissions` form field — the harness-side half of the
Casbin permission contract (see poc-wiki/incremental-development/
0003-user-permission-contract-design.md).

Same two departures from the sibling verify scripts' usual pattern as
verify_server_recent_actions.py, for the same reasons (see that file's
module docstring for the full rationale):

1. In-process ASGI transport (`httpx.ASGITransport(app=app)`), not
   `_server_helper.py`'s subprocess-based `running_server()` — a subprocess
   would make this script's own `server.sessions.store` a different
   process's object than the one `/chat` actually mutates.
2. A scripted model (`ScriptedToolCallModel`, no tool calls needed here —
   this is pure form-field plumbing, unrelated to what the model does),
   not a live one — deterministic, no live API cost, no dependency on a
   real cybersierra backend being reachable.

Four scenarios, the last two run as two turns of the SAME session (proving
overwrite-not-merge semantics, matching the design doc's "refreshed every
turn" decision):

1. A `permissions` form field is parsed and stored on `SessionEntry`
   exactly as sent.
2. `permissions` omitted entirely leaves `user_permissions` at its `None`
   default — proves existing callers that don't send this field (e.g.
   verify_server_recent_actions.py's own `_turn()`) aren't broken by its
   addition.
3. A malformed (non-JSON) `permissions` string doesn't 500 the request —
   the turn still completes normally, `user_permissions` just stays unset
   — exercises the `json.JSONDecodeError` guard in `server/app.py`.
4. A second turn in the same session with a *different* `permissions`
   payload overwrites `user_permissions` rather than merging with the
   first — locks in the "refresh every turn" decision (matches the
   backend's own cadence recommendation: its Redis cache already bounds
   staleness, no second staleness window needed client-side).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import patch

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DEEPAGENT_SERVICE_AUTH", "verify-user-permissions-test-secret")
TEST_SERVICE_AUTH = os.environ["DEEPAGENT_SERVICE_AUTH"]

from server.app import app  # noqa: E402 — after env var is set, see above
from server.sessions import store  # noqa: E402 — same in-process object /chat mutates

PLACEHOLDER_TOKEN = "placeholder-token-not-real"

FIRST_PERMISSIONS = {"assessment": ["CREATE", "LIST", "VIEW"], "scan": ["VIEW"]}
SECOND_PERMISSIONS = {"policy": ["APPROVE", "VIEW"]}


class ScriptedToolCallModel(BaseChatModel):
    """No tool calls needed for this script — this is pure form-field
    plumbing, unrelated to what the model does. Copied, not imported, from
    the sibling verify scripts: each one here is a standalone diagnostic
    tool, not a shared test suite."""

    responses: list[AIMessage] = []
    _i: int = 0

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        # See verify_checkpoint_backup_restore.py's identical comment:
        # harness.agent._classify_message_intent's incidental, concurrent
        # resolve_model() call is recognized and short-circuited here
        # without touching self._i, so it can never consume a slot meant
        # for a specific step in the real sequence below.
        last_text = getattr(messages[-1], "content", "") if messages else ""
        if isinstance(last_text, str) and "Classify the shape of this user message" in last_text:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="conversational"))])
        msg = self.responses[self._i]
        self._i += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])

    @property
    def _llm_type(self) -> str:
        return "scripted"


def _scripted_model() -> ScriptedToolCallModel:
    return ScriptedToolCallModel(responses=[AIMessage(content="done")])


async def _turn(
    client: httpx.AsyncClient, session_id: str | None, permissions: str | None
) -> tuple[int, str]:
    """Run one `/chat` turn, optionally with a `permissions` form field.
    Returns `(status_code, raw_sse_text)`."""
    form = {"message": "hello", "access_token": PLACEHOLDER_TOKEN}
    if session_id is not None:
        form["session_id"] = session_id
    if permissions is not None:
        form["permissions"] = permissions
    with patch("harness.agent.resolve_model", return_value=_scripted_model()):
        resp = await client.post("/chat", data=form)
    return resp.status_code, resp.text


def _session_id_from(raw: str) -> str | None:
    for line in raw.splitlines():
        if line.startswith("data:") and '"session_id"' in line:
            return json.loads(line[len("data:") :].strip()).get("session_id")
    return None


async def main() -> int:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=60.0, headers={"X-Service-Auth": TEST_SERVICE_AUTH}
    ) as client:
        # 1. permissions sent -> stored exactly.
        status, raw = await _turn(client, None, json.dumps(FIRST_PERMISSIONS))
        if status != 200:
            print(f"FAIL: turn 1 returned {status}: {raw}")
            return 1
        session_id = _session_id_from(raw)
        if not session_id:
            print(f"FAIL: no session event in turn 1 response:\n{raw[:2000]}")
            return 1
        entry = store.get(session_id)
        if entry is None:
            print("FAIL: no SessionEntry found for session_id after turn 1")
            return 1
        if entry.user_permissions != FIRST_PERMISSIONS:
            print(f"FAIL: user_permissions after turn 1 was {entry.user_permissions!r}, expected {FIRST_PERMISSIONS!r}")
            return 1
        print(f"  ok: turn 1 stored permissions exactly: {entry.user_permissions!r}")

        # 2. permissions omitted (a separate, brand-new session) -> stays None.
        status, raw = await _turn(client, None, None)
        if status != 200:
            print(f"FAIL: turn 2 (no permissions) returned {status}: {raw}")
            return 1
        other_session_id = _session_id_from(raw)
        if not other_session_id:
            print(f"FAIL: no session event in turn 2 response:\n{raw[:2000]}")
            return 1
        other_entry = store.get(other_session_id)
        if other_entry is None:
            print("FAIL: no SessionEntry found for the no-permissions session")
            return 1
        if other_entry.user_permissions is not None:
            print(f"FAIL: user_permissions should be None when the field is omitted, got {other_entry.user_permissions!r}")
            return 1
        print("  ok: omitting `permissions` entirely leaves user_permissions at its None default")

        # 3. malformed (non-JSON) permissions -> doesn't 500, stays unset.
        status, raw = await _turn(client, None, "not valid json{{{")
        if status != 200:
            print(f"FAIL: turn 3 (malformed permissions) returned {status}, expected 200: {raw}")
            return 1
        malformed_session_id = _session_id_from(raw)
        if not malformed_session_id:
            print(f"FAIL: no session event in turn 3 response:\n{raw[:2000]}")
            return 1
        malformed_entry = store.get(malformed_session_id)
        if malformed_entry is None:
            print("FAIL: no SessionEntry found for the malformed-permissions session")
            return 1
        if malformed_entry.user_permissions is not None:
            print(f"FAIL: malformed permissions should leave user_permissions unset, got {malformed_entry.user_permissions!r}")
            return 1
        print("  ok: malformed `permissions` doesn't 500 the request, and leaves user_permissions unset")

        # 4. second turn, SAME session as turn 1, different permissions -> overwrites, not merges.
        status, raw = await _turn(client, session_id, json.dumps(SECOND_PERMISSIONS))
        if status != 200:
            print(f"FAIL: turn 4 (overwrite) returned {status}: {raw}")
            return 1
        entry = store.get(session_id)
        if entry is None:
            print("FAIL: SessionEntry disappeared between turns")
            return 1
        if entry.user_permissions != SECOND_PERMISSIONS:
            print(
                f"FAIL: user_permissions after turn 4 was {entry.user_permissions!r}, "
                f"expected it overwritten to {SECOND_PERMISSIONS!r} (not merged with turn 1's)"
            )
            return 1
        print(f"  ok: turn 4 overwrote (not merged) user_permissions: {entry.user_permissions!r}")

        print("PASS: user_permissions is populated, left None when omitted, tolerant of malformed input, and refreshed (not merged) every turn.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
