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

Scenario: script the model to call `execute` with
`cybersierra auth set-token fake.jwt.token` verbatim. That command matches
the 3-word `_CYBERSIERRA_COMMAND_PATTERN` shape (`auth`/`set-token`/`fake`)
and is denied by harness/sandbox.py's `DENIED_COMMAND_PREFIXES` group-deny
before any subprocess/network call — provable without a live cybersierra
backend being reachable: exit_code must be `DENY_EXIT_CODE` (126), giving a
deterministic `success=False` RecentAction regardless of environment.
Proving the `success=True` path needs a real backend response and is NOT
covered by this script — see the printed note at the end.
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


async def main() -> int:
    fake_model = ScriptedToolCallModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "execute", "args": {"command": DENIED_COMMAND}, "id": "c1"}]),
            AIMessage(content="done"),
        ]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=60.0, headers={"X-Service-Auth": TEST_SERVICE_AUTH}
    ) as client:
        with patch("harness.agent.resolve_model", return_value=fake_model):
            resp = await client.post(
                "/chat",
                data={"message": "run a command", "access_token": PLACEHOLDER_TOKEN},
            )
        if resp.status_code != 200:
            print(f"FAIL: turn returned {resp.status_code}: {resp.text}")
            return 1

        session_id = None
        for line in resp.text.splitlines():
            if line.startswith("data:") and '"session_id"' in line:
                session_id = json.loads(line[len("data:") :].strip()).get("session_id")
                break
        if not session_id:
            print(f"FAIL: no session event in response:\n{resp.text[:2000]}")
            return 1
        print(f"turn ok, session_id={session_id}")

        entry = store.get(session_id)
        if entry is None:
            print("FAIL: no SessionEntry found for session_id after the turn")
            return 1

        if not entry.recent_actions:
            print(f"FAIL: SessionEntry.recent_actions is empty after a turn that ran a tracked command\n{resp.text[:2000]}")
            return 1

        print(f"recent_actions: {list(entry.recent_actions)}")

        matching = [a for a in entry.recent_actions if a.action == "auth set-token fake"]
        if not matching:
            print(f"FAIL: no recent_actions entry for the expected denied command; got {list(entry.recent_actions)}")
            return 1

        if matching[0].success is not False:
            print(f"FAIL: denied command's RecentAction.success was not False: {matching[0]!r}")
            return 1

        print(f"PASS: SessionEntry.recent_actions recorded the denied action with success=False: {matching[0]!r}")
        print(
            "NOTE: success=True is not exercised by this script — that needs a real "
            "cybersierra backend response (CYBERSIERRA_BASE_URL reachable), which is "
            "a separate, not-yet-covered case."
        )
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
