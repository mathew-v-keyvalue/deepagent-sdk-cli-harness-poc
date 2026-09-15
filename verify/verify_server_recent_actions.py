"""SessionEntry.recent_actions actually records real CLI-shaped actions and
whether each one succeeded, surviving across turns server-side.

Two departures from the other verify_server_*.py scripts' usual pattern,
both deliberate:

1. Deliberately NOT using `_server_helper.py`'s `running_server()` — that
   launches the server as a real subprocess, which means this test script's
   own `server.sessions.store` would be a different process's object than
   the one the subprocess actually mutates, making any assertion on it
   hollow. Instead this runs the FastAPI app in-process via `httpx`'s ASGI
   transport, so `server.sessions.store` (imported directly, below) is the
   exact same object `/chat` mutates.

2. Deliberately NOT a live model, unlike verify_server_session_context.py.
   First attempt at this script used a live model instructed to run a
   specific denied command — the model read skills/cyber-sierra/SKILL.md,
   recognized the command as disallowed from its own instructions, and
   simply refused in text without ever calling `execute` at all. Exactly
   the failure mode verify_shell_sandbox_denies.py's own docstring already
   warns about ("a live model choosing not to misbehave would make this
   test vacuous"). Uses that same script's `ScriptedToolCallModel` pattern
   instead — deterministic, no live API cost — patched in via
   `harness.agent.resolve_model` (the exact seam `_build_agent` calls
   through), so this still exercises the real `harness/agent.py`,
   `server/app.py`, and `harness/sandbox.py` code paths end to end; only
   the model's tool-call *decision* is scripted, not the mechanism being
   tested.

Two scenarios, run as two turns of the SAME session — so this also proves
the "survives across turns" claim in `SessionEntry.recent_actions`' own
docstring, not just that a single turn populates it:

Turn 1 (`success=False`): script the model to call `execute` with
`cybersierra auth set-token fake.jwt.token` verbatim. That command matches
the 3-word `_CYBERSIERRA_COMMAND_PATTERN` shape (`auth`/`set-token`/`fake`)
and is denied by harness/sandbox.py's `DENIED_COMMAND_PREFIXES` group-deny
before any subprocess/network call — provable without a live cybersierra
backend being reachable: exit_code must be `DENY_EXIT_CODE` (126), giving a
deterministic `success=False` RecentAction regardless of environment.

Turn 2 (`success=True`): the counterpart branch — a command that is
*allowed*, really runs as a subprocess, and exits 0, so the ToolMessage
carries a real `{"exit_code": 0}` artifact and `stream()` records
`success=True`. Deliberately NOT a real `cybersierra ...` call: that needs a
reachable backend (`CYBERSIERRA_BASE_URL`), which is exactly what made this
branch untestable before. Instead it uses `python3` — an
`ALLOWED_COMMAND_PREFIXES` entry, same trick
verify_server_multi_session_isolation.py already relies on — printing a
cybersierra-shaped string, so `_CYBERSIERRA_COMMAND_PATTERN` (a `search`,
not a `match`) still extracts an action from it.

What turn 2 does and does not prove, stated plainly: it exercises the real
middleware-allow path, the real `AllowlistedShellBackend.execute`
subprocess, the real `exit_code == 0` artifact read in `stream()`'s
`on_chain_end` branch, and the real `SessionEntry` population — end to end,
no stubbing of the mechanism under test. It does NOT prove anything about
how the real `cybersierra` CLI behaves against a live backend; the code
under test only ever reads `artifact["exit_code"]`, and which binary
produced that 0 is not something it can distinguish.
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

os.environ.setdefault("DEEPAGENT_SERVICE_AUTH", "verify-recent-actions-test-secret")
TEST_SERVICE_AUTH = os.environ["DEEPAGENT_SERVICE_AUTH"]

from server.app import app  # noqa: E402 — after env var is set, see above
from server.sessions import store  # noqa: E402 — same in-process object /chat mutates

PLACEHOLDER_TOKEN = "placeholder-token-not-real"
DENIED_COMMAND = "cybersierra auth set-token fake.jwt.token"
DENIED_ACTION = "auth set-token fake"

# Allowed (`python3` prefix), really runs, exits 0, and still carries a
# 3-word cybersierra-shaped substring for _CYBERSIERRA_COMMAND_PATTERN's
# `search` to extract — see the module docstring for why this stands in for
# a real backend call.
ALLOWED_COMMAND = "python3 -c \"print('cybersierra vendor risk list')\""
ALLOWED_ACTION = "vendor risk list"


class ScriptedToolCallModel(BaseChatModel):
    """Deterministically emits one tool call then stops — see module
    docstring for why a live model isn't used here. Copied from
    verify_shell_sandbox_denies.py rather than imported: each verify
    script here is a standalone diagnostic tool, not a shared test suite.
    """

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


def _scripted_model(command: str) -> ScriptedToolCallModel:
    """One `execute` call with `command`, then a plain final answer."""
    return ScriptedToolCallModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "execute", "args": {"command": command}, "id": "c1"}]),
            AIMessage(content="done"),
        ]
    )


async def _turn(client: httpx.AsyncClient, command: str, session_id: str | None) -> tuple[str | None, str]:
    """Run one `/chat` turn whose model deterministically calls `execute`
    with `command`. Returns `(session_id, raw_sse_text)`; `session_id` is
    `None` if the turn failed or emitted no session event.
    """
    form = {"message": "run a command", "access_token": PLACEHOLDER_TOKEN}
    if session_id is not None:
        form["session_id"] = session_id
    with patch("harness.agent.resolve_model", return_value=_scripted_model(command)):
        resp = await client.post("/chat", data=form)
    if resp.status_code != 200:
        print(f"FAIL: turn returned {resp.status_code}: {resp.text}")
        return None, resp.text
    for line in resp.text.splitlines():
        if line.startswith("data:") and '"session_id"' in line:
            return json.loads(line[len("data:") :].strip()).get("session_id"), resp.text
    # Only the first turn of a session emits a `session` event (see
    # server/app.py's `is_new_session` branch), so a resumed turn legitimately
    # has none — carry the caller's id through rather than calling it a failure.
    return session_id, resp.text


def _check(entry, action: str, expected_success: bool) -> bool:
    matching = [a for a in entry.recent_actions if a.action == action]
    if not matching:
        print(f"FAIL: no recent_actions entry for {action!r}; got {list(entry.recent_actions)}")
        return False
    if matching[0].success is not expected_success:
        print(f"FAIL: {action!r} RecentAction.success was not {expected_success}: {matching[0]!r}")
        return False
    print(f"  ok: {action!r} recorded with success={expected_success}: {matching[0]!r}")
    return True


async def main() -> int:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=60.0, headers={"X-Service-Auth": TEST_SERVICE_AUTH}
    ) as client:
        # Turn 1 — denied command, success=False.
        session_id, raw = await _turn(client, DENIED_COMMAND, None)
        if not session_id:
            print(f"FAIL: no session event in response:\n{raw[:2000]}")
            return 1
        print(f"turn 1 (denied) ok, session_id={session_id}")

        entry = store.get(session_id)
        if entry is None:
            print("FAIL: no SessionEntry found for session_id after turn 1")
            return 1
        if not entry.recent_actions:
            print(f"FAIL: SessionEntry.recent_actions is empty after turn 1\n{raw[:2000]}")
            return 1
        print(f"recent_actions after turn 1: {list(entry.recent_actions)}")
        if not _check(entry, DENIED_ACTION, False):
            return 1

        # Turn 2 — same session, allowed command that really runs and exits 0.
        resumed_id, raw = await _turn(client, ALLOWED_COMMAND, session_id)
        if resumed_id is None:
            return 1
        print(f"turn 2 (allowed, exit 0) ok, session_id={resumed_id}")

        entry = store.get(session_id)
        if entry is None:
            print("FAIL: SessionEntry disappeared between turns")
            return 1
        print(f"recent_actions after turn 2: {list(entry.recent_actions)}")

        # Both must be present: turn 2's success=True (the branch that had no
        # live coverage at all before) AND turn 1's entry still there, which is
        # the cross-turn survival the ring buffer exists for.
        if not _check(entry, ALLOWED_ACTION, True):
            return 1
        if not _check(entry, DENIED_ACTION, False):
            print("       ^ turn 1's action did not survive into turn 2 — the ring buffer is not persisting across turns")
            return 1

        print(
            "PASS: both RecentAction branches recorded (denied -> success=False, "
            "real exit 0 -> success=True), and turn 1's action survived into turn 2."
        )
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
