"""Proves harness.agent's bounded-chat-context feature (poc-wiki/
incremental-development/) actually trims and summarizes correctly against
real running code -- not just that CHAT_CONTEXT_WINDOW_TURNS is defined.

Runs 8 turns of a real (scripted) session_id and inspects, after each turn,
what the model actually received -- not what the harness merely claims to
have sent. The scripted model is a router (branches on whether the incoming
prompt looks like a summarization request), not a fixed response queue --
robust to `_BoundedContextMiddleware` calling `resolve_model()` a second
time (for summarization) within the same turn the main graph also calls it,
without needing to predict exact call ordering.

CHAT_CONTEXT_WINDOW_TURNS is 6 in this codebase; this script imports it
directly rather than hardcoding 6, so it stays correct if that constant
ever changes.

Four things proven, in order:

1. At or under the window (turns 1-6): every turn's model call sees ALL
   prior turns verbatim, and `Done.chat_summary` stays None (no-op, matches
   `_render_user_permissions`'s own "nothing sent when there's nothing to
   send" posture) -- this feature does nothing for a normal-length session.
2. Turn 7 (first turn over the window): `Done.chat_summary` becomes
   non-None, `chat_summary_covers_turns` becomes 1, the model call's
   messages no longer include turn 1's raw content, and the system message
   now carries a rendered summary mentioning turn 1.
3. The summarization call itself is genuinely incremental: turn 8's
   summarization request text does NOT re-include turn 1's content (already
   folded in by turn 7) -- only turn 2's, the one newly expiring now.
4. Turn 8: `chat_summary_covers_turns` advances to 2 (not reset, not stuck),
   and the model call's messages have also dropped turn 2's raw content.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

import re

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DEEPAGENT_SERVICE_AUTH", "verify-bounded-chat-context-test-secret")

from harness.agent import CHAT_CONTEXT_WINDOW_TURNS, stream  # noqa: E402

THREAD_ID = "verify-bounded-chat-context"

# Captured as a side effect of the router model below -- the exact message
# list each *main* (non-summarization) model call actually received, most
# recent last. This is the ground truth this script checks against, not
# anything the harness merely reports about itself.
main_call_messages: list[list] = []
summarization_prompts: list[str] = []


class RouterModel(BaseChatModel):
    """Branches on the incoming prompt's shape rather than a fixed response
    queue -- robust to the middleware calling resolve_model() a second time
    (for summarization) within a turn that also makes the main graph's own
    model call, without this script needing to predict exact call order."""

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        last_text = getattr(messages[-1], "content", "") if messages else ""
        if isinstance(last_text, str) and "Update the running summary" in last_text:
            summarization_prompts.append(last_text)
            # Real turn references actually present in this prompt (existing
            # summary + newly-expired section) -- proves what got folded in.
            mentioned_turns = re.findall(r"turn \d+", last_text)
            reply = "Summary so far: " + "; ".join(dict.fromkeys(mentioned_turns))
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])
        if isinstance(last_text, str) and "Classify the shape of this user message" in last_text:
            # harness.agent._classify_message_intent's incidental
            # resolve_model() call -- not a main-turn call, must not land in
            # main_call_messages (that would silently corrupt the "latest
            # main call" assertions below with an unrelated message list).
            # A generic reply is enough since with_structured_output()'s
            # parsing will fail against this custom double anyway --
            # degrades to intent=None, already exercised as a no-op path.
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="conversational"))])
        main_call_messages.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ack"))])

    @property
    def _llm_type(self) -> str:
        return "router"


async def _run_turn(n: int, *, is_first: bool, chat_summary: str, chat_summary_covers_turns: int):
    kwargs = {"session_id": THREAD_ID} if is_first else {"resume": THREAD_ID}
    done = None
    with patch("harness.agent.resolve_model", return_value=RouterModel()):
        async for event in stream(
            f"turn {n}",
            chat_summary=chat_summary,
            chat_summary_covers_turns=chat_summary_covers_turns,
            **kwargs,
        ):
            if type(event).__name__ == "Done":
                done = event
    return done


def _turn_texts(messages: list) -> list[str]:
    return [m.content for m in messages if isinstance(getattr(m, "content", None), str)]


async def main() -> int:
    chat_summary = ""
    chat_summary_covers_turns = 0

    for n in range(1, CHAT_CONTEXT_WINDOW_TURNS + 1):
        done = await _run_turn(
            n, is_first=(n == 1), chat_summary=chat_summary, chat_summary_covers_turns=chat_summary_covers_turns
        )
        if done.chat_summary is not None:
            print(f"FAIL: turn {n} (at/under window) should not have touched chat_summary, got {done.chat_summary!r}")
            return 1
        latest_call = main_call_messages[-1]
        texts = _turn_texts(latest_call)
        if not any(t == "turn 1" for t in texts):
            print(f"FAIL: turn {n}'s model call should still include turn 1 verbatim (at/under window), got: {texts!r}")
            return 1
    print(f"  ok: turns 1-{CHAT_CONTEXT_WINDOW_TURNS} (at/under the window) are a no-op — full history every time, chat_summary stays None")

    # Turn N+1: first turn over the window.
    done = await _run_turn(
        CHAT_CONTEXT_WINDOW_TURNS + 1,
        is_first=False,
        chat_summary=chat_summary,
        chat_summary_covers_turns=chat_summary_covers_turns,
    )
    if done.chat_summary is None:
        print(f"FAIL: turn {CHAT_CONTEXT_WINDOW_TURNS + 1} should have triggered summarization, chat_summary is still None")
        return 1
    if done.chat_summary_covers_turns != 1:
        print(f"FAIL: expected chat_summary_covers_turns == 1 after the first over-window turn, got {done.chat_summary_covers_turns}")
        return 1
    if "turn 1" not in done.chat_summary:
        print(f"FAIL: the new summary should mention turn 1's content, got: {done.chat_summary!r}")
        return 1
    latest_call = main_call_messages[-1]
    texts = _turn_texts(latest_call)
    if any(t == "turn 1" for t in texts):
        print(f"FAIL: turn 1's raw content should have been trimmed from the model call by now, still present: {texts!r}")
        return 1
    system_texts = [m.content for m in latest_call if type(m).__name__ == "SystemMessage"]
    if not system_texts or "turn 1" not in system_texts[0]:
        print(f"FAIL: the system message should carry the rendered summary mentioning turn 1: {system_texts!r}")
        return 1
    chat_summary, chat_summary_covers_turns = done.chat_summary, done.chat_summary_covers_turns
    print(f"  ok: turn {CHAT_CONTEXT_WINDOW_TURNS + 1} trimmed turn 1's raw content and injected a summary mentioning it")

    # Turn N+2: second over-window turn -- must be incremental, not a full re-summarize.
    done = await _run_turn(
        CHAT_CONTEXT_WINDOW_TURNS + 2,
        is_first=False,
        chat_summary=chat_summary,
        chat_summary_covers_turns=chat_summary_covers_turns,
    )
    last_summarization_prompt = summarization_prompts[-1]
    if "turn 1" in last_summarization_prompt.split("New turn to fold in:")[-1]:
        print(f"FAIL: turn {CHAT_CONTEXT_WINDOW_TURNS + 2}'s summarization re-included turn 1 in the 'newly expired' section — not incremental")
        return 1
    if "turn 2" not in last_summarization_prompt.split("New turn to fold in:")[-1]:
        print(f"FAIL: turn {CHAT_CONTEXT_WINDOW_TURNS + 2}'s summarization should fold in turn 2 specifically")
        return 1
    if done.chat_summary_covers_turns != 2:
        print(f"FAIL: expected chat_summary_covers_turns == 2 after the second over-window turn, got {done.chat_summary_covers_turns}")
        return 1
    latest_call = main_call_messages[-1]
    texts = _turn_texts(latest_call)
    if any(t in ("turn 1", "turn 2") for t in texts):
        print(f"FAIL: turns 1-2's raw content should both be trimmed by now, still present: {texts!r}")
        return 1
    print(f"  ok: turn {CHAT_CONTEXT_WINDOW_TURNS + 2} folded in only the newly-expired turn (incremental, not a full re-summarize), covers_turns advanced to 2")

    print(
        "PASS: bounded chat context is a true no-op at/under the window, trims and summarizes correctly once "
        "exceeded, and each summarization step folds in only the newly-expired turn, not the whole history."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
