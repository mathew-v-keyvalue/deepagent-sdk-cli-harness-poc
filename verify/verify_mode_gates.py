"""Execution modes (Ask/Agent-Plan/Agent-Auto) actually gate what they
should — the counterpart to `verify_shell_sandbox_denies.py` for the mode
work described in `poc-wiki/execution-modes/`.

Same honesty standard as that script: nothing here trusts "no exception
was thrown." Each check inspects a real side effect, a real `ToolMessage`
status, or real graph state (`graph.aget_state`'s pending interrupts) --
and where a real CLI call is exercised, it's the actual installed
`cybersierra` binary, not a mock.

Why a scripted model instead of a live one, again: this needs to
*guarantee* the model attempts specific tool calls, deterministically, on
every run -- see `verify_shell_sandbox_denies.py`'s module docstring for
the full reasoning, identical here. Only the model is mocked; sandbox,
middleware, interrupt_on, the real executor tool, and the real CLI
subprocess boundary are all exercised for real.

Two different techniques are used for two different questions, on
purpose, not interchangeably:

- **Denial checks** (a middleware-denied call) build the real graph
  directly and call `graph.ainvoke`, inspecting the resulting
  `ToolMessage.status` -- confirmed empirically that a denied call
  produces NEITHER `on_tool_start` NOR `on_tool_end` via
  `astream_events`, independent of anything this work changed (this is
  true even for today's existing single-mode behavior). `stream()`'s SSE
  events are the wrong signal to check for a denial.
- **Pause/resume checks** (`interrupt_on`) drive the real
  `harness.agent.stream()` -- the exact function `server/app.py` calls --
  since a pending interrupt (unlike a denial) genuinely does surface
  through it (`AwaitingApproval`).

Checks, in order:
1. Prerequisite-fix regression: an all-safe plan succeeds in `agent_auto`
   (guards against re-breaking the fix that made `run_execution_plan`
   reachable at all).
2. Hard-deny in `ask`/`agent_plan`: an unsafe plan is denied outright
   (not paused), ad-hoc `execute` is denied outright, and a skill-write
   is denied in `ask` but reaches a *pending interrupt* in `agent_plan`
   (its one approval exception).
3. Middleware-order de-risk in `agent_auto`: an unsafe plan reaches a
   pending interrupt (not silently denied or executed); a flatly-
   disallowed raw command still denies outright, un-paused, even with a
   real `interrupt_on` config present in the same graph.
4. End-to-end approve -> unlock: pause, approve, confirm the plan's real
   CLI call actually ran, then confirm a *second* unsafe plan in the same
   session does not pause again.
5. Reject -> re-pause: pause, reject, confirm nothing ran and the next
   unsafe-plan attempt still pauses (unlock was never granted).
6. Skill-write always-asks, independent of `write_unlocked`: even in an
   already-unlocked `agent_auto` session, a skill-write still pauses.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from deepagents import create_deep_agent
from harness.executor_tool import make_run_execution_plan_tool
from harness.sandbox import AllowlistedShellBackend, ShellSandboxMiddleware


class ScriptedToolCallModel(BaseChatModel):
    """See verify_shell_sandbox_denies.py's identical class for why a
    scripted (not live) model is the honest choice for this kind of check.
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


def _plan(safe: bool, plan_id: str = "p1") -> str:
    return json.dumps(
        {
            "planId": plan_id,
            "source": "planner",
            "sourceName": "test",
            "steps": [{"id": 1, "command": "auth whoami", "arguments": {}, "reason": "test step", "safe": safe}],
        }
    )


def _tool_call(name: str, args: dict, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _run_execution_plan_call(safe: bool, call_id: str = "c1") -> AIMessage:
    return _tool_call("run_execution_plan", {"plan_json": _plan(safe)}, call_id)


async def _ainvoke_direct(mode: str, tool_call: dict, *, interrupt_on: dict | None = None):
    """Builds the real graph directly (same pattern as
    verify_shell_sandbox_denies.py's check_middleware_layer) and returns the
    resulting `ToolMessage` -- the reliable way to confirm a hard deny, since
    a denied call never produces on_tool_start/on_tool_end via
    astream_events (see module docstring).
    """
    scripted = ScriptedToolCallModel(responses=[AIMessage(content="", tool_calls=[tool_call]), AIMessage(content="done")])
    backend = AllowlistedShellBackend(root_dir=tempfile.mkdtemp(), env={"PATH": os.environ["PATH"]})
    graph = create_deep_agent(
        model=scripted,
        tools=[make_run_execution_plan_tool(backend)],
        middleware=[ShellSandboxMiddleware(mode=mode)],
        backend=backend,
        checkpointer=InMemorySaver(),
        interrupt_on=interrupt_on,
    )
    config = {"configurable": {"thread_id": f"direct-{tool_call['id']}"}}
    result = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, config)
    tool_messages = [m for m in result["messages"] if type(m).__name__ == "ToolMessage"]
    assert tool_messages, f"no ToolMessage produced for {tool_call!r}"
    return tool_messages[0]


async def _stream_all(mode: str, prompt_calls: list[AIMessage], **stream_kwargs) -> tuple[list, object]:
    """Monkeypatches only the model (see module docstring) and drives the
    REAL `harness.agent.stream()` -- the exact function `server/app.py`
    calls -- collecting every event it yields. `prompt_calls` is the
    scripted model's FULL response list for this call -- including for a
    resume: the model is invoked again once the resumed tool call finishes,
    so a resume that shouldn't trigger another tool call needs an empty
    list here, not a placeholder tool call (a placeholder gets consumed for
    real, as if the model actually chose to call it again).
    """
    from harness import agent as agent_module

    scripted = ScriptedToolCallModel(responses=[*prompt_calls, AIMessage(content="done")])
    # Patched on harness.agent's own name, not harness.model's -- agent.py
    # does `from harness.model import resolve_model`, binding its own local
    # name at import time. Patching harness.model.resolve_model after
    # harness.agent has already been imported (cached in sys.modules, which
    # it will be by the time any check here runs) has no effect at all.
    agent_module.resolve_model = lambda: scripted
    stream_kwargs.setdefault("prompt", "")  # ignored by stream() when resuming with a decision
    events = []
    async for event in agent_module.stream(mode=mode, **stream_kwargs):
        events.append(event)
    return events, agent_module


async def check_prerequisite_fix_regression() -> bool:
    from harness import agent as agent_module

    events, _ = await _stream_all(
        "agent_auto", [_run_execution_plan_call(safe=True)], prompt="check", session_id="pfr-1"
    )
    finished = [e for e in events if isinstance(e, agent_module.ToolUseFinished)]
    awaiting = [e for e in events if isinstance(e, agent_module.AwaitingApproval)]
    if awaiting:
        print("FAIL (prerequisite regression): an all-safe plan should never pause, but it did")
        return False
    if not finished or not finished[0].success:
        print(f"FAIL (prerequisite regression): all-safe plan did not complete successfully: {finished!r}")
        return False
    print("PASS (prerequisite regression): all-safe run_execution_plan call succeeds in agent_auto, unpaused")
    return True


async def check_hard_deny_ask_and_plan() -> bool:
    from harness import agent as agent_module

    ok = True
    for mode in ("ask", "agent_plan"):
        msg = await _ainvoke_direct(mode, {"name": "run_execution_plan", "args": {"plan_json": _plan(safe=False)}, "id": f"plan-{mode}"})
        if getattr(msg, "status", None) != "error":
            print(f"FAIL (hard-deny): {mode!r} did not deny the unsafe plan: {msg!r}")
            ok = False
        else:
            print(f"PASS (hard-deny): {mode!r} denies an unsafe run_execution_plan call outright")

        msg = await _ainvoke_direct(mode, {"name": "execute", "args": {"command": "cybersierra auth whoami"}, "id": f"exec-{mode}"})
        if getattr(msg, "status", None) != "error":
            print(f"FAIL (hard-deny): {mode!r} did not deny an ad-hoc execute call: {msg!r}")
            ok = False
        else:
            print(f"PASS (hard-deny): {mode!r} denies ad-hoc execute outright")

    skill_write_args = {"file_path": "skills/_generated/foo/SKILL.md", "content": "..."}
    msg = await _ainvoke_direct("ask", {"name": "write_file", "args": skill_write_args, "id": "skillwrite-ask"})
    if getattr(msg, "status", None) != "error":
        print(f"FAIL (hard-deny): ask should hard-deny a skill-write with no exception: {msg!r}")
        ok = False
    else:
        print("PASS (hard-deny): ask hard-denies a skill-write, no exception")

    # agent_plan: skill-write reaches a pending interrupt instead (its one
    # exception) -- this goes through stream()/interrupt_on for real, unlike
    # the ainvoke-direct checks above (which don't wire interrupt_on at all).
    events, _ = await _stream_all(
        "agent_plan", [_tool_call("write_file", skill_write_args, call_id="c4")], prompt="learn a skill", session_id="hd-skill-plan"
    )
    if not any(isinstance(e, agent_module.AwaitingApproval) for e in events):
        print("FAIL (hard-deny): agent_plan should let a skill-write reach a pending interrupt, but it didn't")
        ok = False
    else:
        print("PASS (hard-deny): agent_plan lets a skill-write pause for approval instead of denying it")

    return ok


async def check_middleware_order_derisk() -> bool:
    from harness import agent as agent_module

    events, _ = await _stream_all(
        "agent_auto", [_run_execution_plan_call(safe=False)], prompt="deactivate something", session_id="derisk-1"
    )
    awaiting = [e for e in events if isinstance(e, agent_module.AwaitingApproval)]
    finished = [e for e in events if isinstance(e, agent_module.ToolUseFinished)]
    ok = True
    if not awaiting:
        print("FAIL (middleware-order): unsafe plan in agent_auto did not reach a pending interrupt")
        ok = False
    elif finished:
        print(f"FAIL (middleware-order): unsafe plan should pause before ever finishing, but it finished: {finished!r}")
        ok = False
    else:
        print("PASS (middleware-order): unsafe plan in agent_auto reaches a real pending interrupt, unfinished")

    # Real interrupt_on config present in the same graph, same as agent_auto
    # actually builds -- confirms "execute" (not a key in interrupt_on at
    # all) still denies outright through ShellSandboxMiddleware, unaffected
    # by interrupt_on's presence.
    real_interrupt_on = agent_module._build_interrupt_on("agent_auto", write_unlocked=False)
    msg = await _ainvoke_direct(
        "agent_auto",
        {"name": "execute", "args": {"command": "cybersierra auth login-browser"}, "id": "derisk-exec"},
        interrupt_on=real_interrupt_on,
    )
    if getattr(msg, "status", None) != "error":
        print(f"FAIL (middleware-order): flatly-disallowed command was not denied: {msg!r}")
        ok = False
    else:
        print("PASS (middleware-order): flatly-disallowed command still denies outright in agent_auto, even alongside a real interrupt_on config")

    return ok


async def check_approve_then_unlock() -> bool:
    from harness import agent as agent_module

    session_id = "approve-1"
    events1, _ = await _stream_all(
        "agent_auto", [_run_execution_plan_call(safe=False, call_id="c1")], prompt="deactivate", session_id=session_id
    )
    if not any(isinstance(e, agent_module.AwaitingApproval) for e in events1):
        print("FAIL (approve->unlock): first unsafe plan did not pause")
        return False

    # Empty list: after the resumed tool call finishes, the model is invoked
    # again for its next turn -- it should just say "done", not attempt
    # another tool call (a placeholder here gets consumed for real).
    events2, _ = await _stream_all(
        "agent_auto", [], resume=session_id, write_unlocked=False, decision={"type": "approve"}
    )
    plan_steps = [e for e in events2 if isinstance(e, agent_module.PlanStepFinished)]
    done = [e for e in events2 if isinstance(e, agent_module.Done)]
    if not plan_steps or not done:
        print(f"FAIL (approve->unlock): approving did not lead to real execution: plan_steps={plan_steps!r} done={done!r}")
        return False
    print(f"PASS (approve->unlock): approval resumed real execution ({len(plan_steps)} step(s) ran, turn completed)")

    # Second unsafe plan, same session, now with write_unlocked=True (as
    # server/app.py's /decide would set on the session after approval) --
    # must NOT pause again.
    events3, _ = await _stream_all(
        "agent_auto",
        [_run_execution_plan_call(safe=False, call_id="c3")],
        prompt="deactivate another one",
        session_id="approve-1-turn2",  # fresh thread_id: this check targets the write_unlocked flag itself, not checkpointer state
        write_unlocked=True,
    )
    if any(isinstance(e, agent_module.AwaitingApproval) for e in events3):
        print("FAIL (approve->unlock): a second unsafe plan paused again despite write_unlocked=True")
        return False
    print("PASS (approve->unlock): with write_unlocked=True, a second unsafe plan does not pause again")
    return True


async def check_reject_then_repause() -> bool:
    from harness import agent as agent_module

    session_id = "reject-1"
    events1, _ = await _stream_all(
        "agent_auto", [_run_execution_plan_call(safe=False, call_id="c1")], prompt="deactivate", session_id=session_id
    )
    if not any(isinstance(e, agent_module.AwaitingApproval) for e in events1):
        print("FAIL (reject->repause): first unsafe plan did not pause")
        return False

    events2, _ = await _stream_all(
        "agent_auto", [], resume=session_id, write_unlocked=False, decision={"type": "reject", "message": "not now"}
    )
    plan_steps = [e for e in events2 if isinstance(e, agent_module.PlanStepFinished)]
    if plan_steps:
        print(f"FAIL (reject->repause): rejected plan should not have run any step, but it did: {plan_steps!r}")
        return False
    print("PASS (reject->repause): rejecting runs nothing")

    # write_unlocked stays False after a reject (server/app.py never sets it
    # on reject) -- the next unsafe-plan attempt in this session must pause again.
    events3, _ = await _stream_all(
        "agent_auto",
        [_run_execution_plan_call(safe=False, call_id="c3")],
        prompt="try again",
        session_id="reject-1-turn2",
        write_unlocked=False,
    )
    if not any(isinstance(e, agent_module.AwaitingApproval) for e in events3):
        print("FAIL (reject->repause): next unsafe-plan attempt should still pause after a reject, but it didn't")
        return False
    print("PASS (reject->repause): next unsafe-plan attempt still pauses -- reject never unlocks")
    return True


async def check_skill_write_always_asks_even_when_unlocked() -> bool:
    from harness import agent as agent_module

    skill_write_args = {"file_path": "skills/_generated/bar/SKILL.md", "content": "..."}
    events, _ = await _stream_all(
        "agent_auto",
        [_tool_call("write_file", skill_write_args)],
        prompt="learn a new skill",
        session_id="skillwrite-unlocked",
        write_unlocked=True,  # session already unlocked for ordinary writes
    )
    if not any(isinstance(e, agent_module.AwaitingApproval) for e in events):
        print("FAIL (skill-write always-asks): a skill-write did not pause even though write_unlocked=True")
        return False
    print("PASS (skill-write always-asks): a skill-write still pauses in agent_auto regardless of write_unlocked")
    return True


def main() -> int:
    checks = [
        check_prerequisite_fix_regression,
        check_hard_deny_ask_and_plan,
        check_middleware_order_derisk,
        check_approve_then_unlock,
        check_reject_then_repause,
        check_skill_write_always_asks_even_when_unlocked,
    ]
    results = [asyncio.run(check()) for check in checks]
    if all(results):
        print("\nPASS: execution modes gate reads/writes/skill-writes correctly across ask/agent_plan/agent_auto")
        return 0
    print("\nFAIL: one or more mode-gating checks failed -- see above")
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
