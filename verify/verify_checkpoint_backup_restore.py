"""Proves the durable-checkpointer design (poc-wiki/incremental-
development/) actually works against a real Postgres instance, not just
that the code compiles.

Earlier design attempt (kept InMemorySaver as a hot per-step layer, with a
*separate* Postgres "backup" synced by hand at turn boundaries) turned out
to be broken: LangGraph's `messages` channel is delta-based, and copying
only the latest checkpoint's `channel_values` between two independent
checkpointer instances drops the ancestor chain delta reconstruction
depends on -- confirmed live (restored state silently had zero messages),
not guessed. This script verifies the corrected design instead: swap
`harness.agent._checkpointer` for a real `AsyncPostgresSaver` outright (via
`set_checkpointer`, exactly as `server/app.py`'s lifespan hook does), same
one instance used for every read and write, no manual cross-backend
copying at all.

Uses the real local morpheus_backend Postgres (DATABASE_URL in .env) and a
scripted model (deterministic, no live LLM cost -- this is testing
persistence plumbing, not model reasoning).

Two things proven:

1. A turn run against the Postgres-backed checkpointer, followed by a
   second turn built from a *fresh* `_build_agent()` call (simulating a new
   process picking up the same session_id -- the only thing that's
   literally identical across process restarts is the underlying Postgres
   storage, not any in-memory object), genuinely has both turns' real
   content -- not an empty/reset conversation.
2. `set_checkpointer`/the module-level swap itself works as intended: after
   swapping, `harness.agent.stream()` (the actual public function
   server/app.py calls, not an internal helper) uses the swapped-in
   checkpointer, and turns run through it land in the real Postgres tables.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

import harness.agent as agent_module  # noqa: E402 — after sys.path/load_dotenv, see above

DATABASE_URL = os.environ.get("DATABASE_URL")
THREAD_ID = "verify-checkpoint-postgres-swap"


class ScriptedToolCallModel(BaseChatModel):
    """No tool calls needed -- this is pure persistence plumbing, unrelated
    to what the model does. Copied, not imported, from the sibling verify
    scripts."""

    responses: list[AIMessage] = []
    _i: int = 0

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        # harness.agent._classify_message_intent also calls resolve_model()
        # (mocked to this same instance) as an incidental background call,
        # running *concurrently* with the main turn -- asyncio scheduling
        # doesn't guarantee it lands before/after/between the real,
        # intentionally-sequenced calls below, so merely clamping the index
        # isn't safe (it could still consume a slot meant for a specific
        # step). Recognized and short-circuited without touching self._i at
        # all, so the real sequence is untouched regardless of timing.
        last_text = getattr(messages[-1], "content", "") if messages else ""
        if isinstance(last_text, str) and "Classify the shape of this user message" in last_text:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="conversational"))])
        msg = self.responses[self._i]
        self._i += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])

    @property
    def _llm_type(self) -> str:
        return "scripted"


def _scripted_model(reply: str) -> ScriptedToolCallModel:
    return ScriptedToolCallModel(responses=[AIMessage(content=reply)])


async def _run_turn(prompt: str, reply: str, *, is_first: bool) -> None:
    kwargs = {"session_id": THREAD_ID} if is_first else {"resume": THREAD_ID}
    with patch("harness.agent.resolve_model", return_value=_scripted_model(reply)):
        async for _event in agent_module.stream(prompt, **kwargs):
            pass  # draining the generator is what actually runs the turn


async def main() -> int:
    if not DATABASE_URL:
        print("FAIL: DATABASE_URL not set in .env -- can't test against real Postgres")
        return 1

    async with AsyncPostgresSaver.from_conn_string(DATABASE_URL) as pg:
        await pg.setup()
        await pg.adelete_thread(THREAD_ID)  # clean slate in case a prior failed run left state

        agent_module.set_checkpointer(pg)
        try:
            # Turn 1: via the real public stream() function, not an internal helper.
            await _run_turn("remember the word pineapple", "ok, noted: pineapple", is_first=True)

            # Turn 2: resume the same thread_id. Nothing here reuses any
            # Python object from turn 1 beyond the module-level swap itself
            # (which is exactly what would be true across a real process
            # restart too, since `pg` here is the one persistent thing --
            # the actual Postgres tables, not anything in-process).
            await _run_turn("what word did I ask you to remember?", "you said pineapple", is_first=False)

            config = {"configurable": {"thread_id": THREAD_ID}}
            graph = agent_module._build_agent("")  # fresh graph object, same module-level checkpointer
            state = await graph.aget_state(config)
            texts = [getattr(m, "content", "") for m in state.values.get("messages", [])]

            if not any("pineapple" in t for t in texts if isinstance(t, str) and "noted" in t):
                print(f"FAIL: turn 1's assistant reply not found in state after turn 2: {texts!r}")
                return 1
            if not any(t == "what word did I ask you to remember?" for t in texts):
                print(f"FAIL: turn 2's own message not found in state: {texts!r}")
                return 1
            print(f"  ok: both turns' real content present after resume — {len(texts)} messages total")

            # Confirm this really landed in Postgres, not just process memory.
            direct = await pg.aget_tuple(config)
            if direct is None:
                print("FAIL: nothing found querying Postgres directly for this thread_id")
                return 1
            print("  ok: state is genuinely readable straight from Postgres, not just via the graph object")

        finally:
            await pg.adelete_thread(THREAD_ID)
            agent_module.set_checkpointer(InMemorySaver())  # restore the harness's normal default

    print(
        "PASS: swapping in a real AsyncPostgresSaver via set_checkpointer() preserves genuine "
        "multi-turn continuity, readable directly from Postgres — the corrected design works."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
