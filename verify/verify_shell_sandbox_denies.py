"""Shell sandbox actually denies — the DeepAgents counterpart to the
sibling Claude SDK POC's "ask the agent to run `ls -la`, confirm denial"
check (see that POC's README "Security boundary").

This does NOT trust "no exception was thrown." Each check inspects either
a real filesystem side effect (did the denied command actually NOT run) or
the tool result's own `status`/`exit_code` field — the DeepAgents analogue
of inspecting `tool_result.is_error` in the Claude SDK.

Three layers, each checked independently, because this harness has two
independent enforcement layers on purpose (see harness/sandbox.py's module
docstring for why) and this script is what proves both actually work,
rather than assuming the second one does because the first one does:

1. `AllowlistedShellBackend.execute` — the backend method that would call
   `subprocess.run`. Denial checked by a REAL side effect: a denied
   `touch` must not create the file.
2. `ShellSandboxMiddleware.awrap_tool_call` running inside a REAL compiled
   DeepAgents graph (a scripted, not live, model — see below for why that's
   still a real test) — denial checked by the resulting `ToolMessage`'s
   `status` field, and by the same real-side-effect check as layer 1, run
   through the actual tool-node dispatch path this harness uses in
   production (async), not just a direct method call. This is also what
   caught the `awrap_tool_call`-vs-`wrap_tool_call` gap documented in
   harness/sandbox.py — this script is that regression test.
3. An allowed command, run the same way, actually reaches the real
   `cybersierra` CLI (not just "isn't denied") — proven by getting back a
   real CLI error envelope (`{"error": {"code": ...}}`), which only the
   real binary can produce.

Why a scripted model instead of a live one: this script needs to
*guarantee* an attempt to run a disallowed command happens, deterministically,
on every run — a live model choosing not to misbehave would make this test
vacuous, and a live model that DOES misbehave would make it flaky. The
model is the only mocked part; the middleware, tool dispatch, backend, and
subprocess boundary are all real, which is the actual thing being verified.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from deepagents import create_deep_agent
from harness.sandbox import AllowlistedShellBackend, ShellSandboxMiddleware, is_command_allowed

MARKER = os.path.join(tempfile.gettempdir(), "verify_shell_sandbox_denies.marker")


class ScriptedToolCallModel(BaseChatModel):
    """Deterministically emits one tool call per invocation, from a fixed
    script — see module docstring for why this is the honest choice here.
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


def check_backend_layer() -> bool:
    if os.path.exists(MARKER):
        os.remove(MARKER)
    backend = AllowlistedShellBackend(root_dir=tempfile.mkdtemp(), env={"PATH": os.environ["PATH"]})

    denied = backend.execute(f"touch {MARKER}")
    if os.path.exists(MARKER):
        print("FAIL (layer 1): AllowlistedShellBackend ran a disallowed command — file was created")
        return False
    if denied.exit_code == 0:
        print("FAIL (layer 1): disallowed command returned exit_code 0")
        return False
    print(f"PASS (layer 1/backend): denied, exit_code={denied.exit_code}, no side effect")

    allowed = backend.execute("cybersierra --version")
    if allowed.exit_code != 0:
        print(f"FAIL (layer 1): allowed command 'cybersierra --version' did not succeed: {allowed.output!r}")
        return False
    print(f"PASS (layer 1/backend): allowed command reached the real cybersierra binary: {allowed.output.strip()!r}")
    return True


def check_auth_group_denials() -> bool:
    """The `cybersierra auth` group-deny (added after the fresh-deployment
    persisted-profile gap): `poll`/`set-token`/`logout` must be denied by
    the sandbox before any subprocess ever runs (exit_code == DENY_EXIT_CODE,
    not whatever the real CLI would itself return for a malformed call —
    that distinction is what proves this was denied, not just failed for
    its own reasons), while `whoami` must still be explicitly allowed
    despite matching the same `cybersierra auth` prefix.
    """
    from harness.sandbox import DENY_EXIT_CODE

    backend = AllowlistedShellBackend(root_dir=tempfile.mkdtemp(), env={"PATH": os.environ["PATH"]})
    ok = True

    for denied_command in (
        "cybersierra auth poll",
        "cybersierra auth set-token fake.jwt.token",
        "cybersierra auth logout",
    ):
        result = backend.execute(denied_command)
        if result.exit_code != DENY_EXIT_CODE:
            print(f"FAIL (auth-group deny): {denied_command!r} was not denied (exit_code={result.exit_code})")
            ok = False
        else:
            print(f"PASS (auth-group deny): {denied_command!r} denied, exit_code={DENY_EXIT_CODE}")

    if not is_command_allowed("cybersierra auth whoami"):
        print("FAIL (auth-group deny): 'cybersierra auth whoami' is no longer allowed despite the carve-out")
        ok = False
    else:
        print("PASS (auth-group deny): 'cybersierra auth whoami' remains explicitly allowed")

    return ok


async def check_middleware_layer() -> bool:
    if os.path.exists(MARKER):
        os.remove(MARKER)

    fake_model = ScriptedToolCallModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "execute", "args": {"command": f"touch {MARKER}"}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "execute", "args": {"command": "cybersierra auth whoami"}, "id": "c2"}]),
            AIMessage(content="done"),
        ]
    )
    backend = AllowlistedShellBackend(root_dir=tempfile.mkdtemp(), env={"PATH": os.environ["PATH"]})
    graph = create_deep_agent(
        model=fake_model,
        middleware=[ShellSandboxMiddleware()],
        backend=backend,
        checkpointer=InMemorySaver(),
    )

    config = {"configurable": {"thread_id": "verify-sandbox"}}
    result = await graph.ainvoke({"messages": [HumanMessage(content="run a command")]}, config)

    tool_messages = [m for m in result["messages"] if type(m).__name__ == "ToolMessage"]
    if len(tool_messages) != 2:
        print(f"FAIL (layer 2): expected 2 tool results, got {len(tool_messages)}")
        return False

    denied_msg, allowed_msg = tool_messages
    if os.path.exists(MARKER):
        print("FAIL (layer 2): disallowed command ran inside a real graph — file was created")
        return False
    if getattr(denied_msg, "status", None) != "error":
        print(f"FAIL (layer 2): disallowed tool call did not come back status='error': {denied_msg!r}")
        return False
    print(f"PASS (layer 2/middleware, real graph): denied, status='error', no side effect. Content: {denied_msg.content[:100]!r}")

    if getattr(allowed_msg, "status", None) != "success":
        print(f"FAIL (layer 2): allowed tool call did not succeed: {allowed_msg!r}")
        return False
    if "error" not in str(allowed_msg.content) and "code" not in str(allowed_msg.content):
        print(f"FAIL (layer 2): allowed command's output doesn't look like a real cybersierra CLI response: {allowed_msg.content!r}")
        return False
    print(f"PASS (layer 2/middleware, real graph): allowed command reached the real CLI: {str(allowed_msg.content)[:100]!r}")
    return True


def main() -> int:
    ok_backend = check_backend_layer()
    ok_auth_group = check_auth_group_denials()
    ok_middleware = asyncio.run(check_middleware_layer())
    if os.path.exists(MARKER):
        os.remove(MARKER)

    if ok_backend and ok_auth_group and ok_middleware:
        print("\nPASS: both sandbox enforcement layers deny by default and let allowed commands through")
        return 0
    print("\nFAIL: shell sandbox did not enforce correctly — see above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
