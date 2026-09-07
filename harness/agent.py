"""Generic DeepAgents-style agent harness — the DeepAgents counterpart to
the sibling Claude SDK POC's `harness/agent.py`.

This module knows nothing about cybersierra specifically: no command names,
no endpoints, no auth flow. Everything domain-specific lives in
`skills/cyber-sierra/SKILL.md` (a verbatim copy of the real, already-built
plugin skill — see README "Porting the real pipeline"), discovered at
runtime by DeepAgents' own `SkillsMiddleware`. Adding skill #2 means adding
a new directory under `skills/`; this file does not change — same rule the
Claude SDK POC's harness follows for `.claude/skills/`.

Wires up, once per call, exactly like the Claude POC's `_build_options`:
model resolution (harness/model.py), the two-layer shell sandbox
(harness/sandbox.py), the deterministic Executor tool
(harness/executor_tool.py), skills discovery, and a LangGraph checkpointer
for session continuity — the DeepAgents analogue of the Claude SDK's own
`session_id`/`resume`. See README "Session continuity: checkpointer vs
on-disk transcript" for exactly how that analogy holds and where it
doesn't.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from harness.executor_tool import make_run_execution_plan_tool
from harness.model import resolve_model
from harness.sandbox import AllowlistedShellBackend, ShellSandboxMiddleware, scrub
from harness.tracing import Netra, SpanType, trace_content_enabled

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = PROJECT_ROOT / "skills"

# "harness.agent" nests under the "harness" logger that
# harness/observability.py's configure_logging() attaches a real handler
# to. Every log line below is the actual evidence of what this harness did
# on a given call — turn boundaries and skill discovery here;
# harness/sandbox.py separately logs every real CLI/tool call. See README
# "Proper logging".
logger = logging.getLogger("harness.agent")
logger.addHandler(logging.NullHandler())

# Generic-only, same spirit as the Claude POC's SYSTEM_PROMPT_APPENDIX: no
# mention of cybersierra, compliance, or any specific command. Two
# additions that ARE harness-specific (not domain-specific): the tool-name
# bridge (the real skill's frontmatter and body were authored for Claude
# Code's tool names — `Bash`, `Read`, `Write` — and DeepAgents' names differ
# — `execute`, `read_file`, `write_file`; rather than edit the ported skill
# files, see skills/cyber-sierra/, copied verbatim on purpose as primary
# source, we bridge the naming gap here), and the "read before guessing" /
# discovery-escalation lines — added after dataset/README.md's spot-checking
# caught the model guessing plausible-but-wrong CLI subcommands (`cybersierra
# vendors list`, `cybersierra get vendors`, ...) instead of reading the
# loaded skill fully first, even though that skill's own text already says
# to discover the real command surface before acting (see dataset/README.md
# "Observed limitation" for the before/after log evidence).
#
# A third addition — telling the model to skip the plan-confirmation step
# for read-only plans — was tried and then deliberately removed. It bypassed
# the real skill's own Step 4 ("Present Plan & Confirm") gate, which the
# rest of this port treats as sacrosanct (see README "Porting the real
# pipeline" — the soft, instruction-based confirmation gate is called out
# explicitly as intentional, matching the source system's own design, not
# something to route around at the harness level). If read-only plans
# stopping to confirm is a real problem, the right fix is resolving the
# contradiction inside skills/cyber-sierra/ itself (a disclosed exception to
# "copied verbatim"), not overriding confirmation behavior from the harness.
SYSTEM_PROMPT_APPENDIX = (
    "If an available skill matches the user's request, use it rather than "
    "answering from general knowledge alone.\n\n"
    "This environment's tool names differ from the ones referenced inside "
    "loaded skill files, which were authored for a different agent runtime. "
    "Map them as follows: `Bash` and `Shell` both mean this environment's "
    "`execute` tool (same shell-command semantics); `Read` means `read_file` "
    "(pass limit=1000 for files longer than 100 lines, per the Skills System "
    "instructions already in this prompt); `Write` means `write_file`.\n\n"
    "Before running any shell command against a domain CLI referenced by a "
    "loaded skill, read that skill's full instructions first with read_file "
    "-- do not guess subcommand names or flags from general knowledge, even "
    "ones that sound plausible. If the skill describes how to discover the "
    "CLI's real command surface (e.g. a manifest or catalog command), run "
    "that discovery step before attempting any other command against that "
    "CLI, and only use commands that discovery step actually returned.\n\n"
    "A discovery command that returns only shallow, high-level results (for "
    "example, top-level category or module names with no further detail) is "
    "not sufficient to act on -- it means you must go one level deeper (e.g. "
    "that CLI's own --help on the specific subcommand or category you just "
    "identified) before trying an actual command, not that you should start "
    "guessing plausible-sounding subcommand or flag names. If two guesses "
    "against the same CLI fail in a row, stop guessing and escalate to that "
    "CLI's own --help instead of trying a third guess; never repeat the "
    "exact same shallow discovery call more than once without escalating."
)

# One checkpointer for the life of this process, shared by every session —
# this IS the state; server/sessions.py (like the Claude POC's) holds only
# a session-id -> lock/metadata mapping, never message history. Process-
# lifetime, in-memory: same deliberate POC-scope choice as the Claude POC's
# SessionStore, made explicit in README "Known limitations".
_checkpointer = InMemorySaver()

# Passed through to the sandboxed shell backend's subprocess env so
# `cybersierra`/`npm`/`npx`/`python3` resolve and the CLI's persisted
# profile (~/.cybersierra/config.json) is found. Nothing else from this
# process's ambient os.environ crosses into the sandbox — see
# harness/sandbox.py and README "Security boundary".
_PASSTHROUGH_ENV_KEYS = ("PATH", "HOME")

# Off by default, on purpose — see README "Authentication model" and the
# note on `_build_agent` below for why. Set to "1" to switch this harness
# into true per-request token isolation (real, verified — see README — but
# requires the UI's `access_token` field to hold an actual cybersierra JWT
# on every request, not a placeholder).
_INJECT_ENV_VAR = "CYBERSIERRA_INJECT_ACCESS_TOKEN"

# LangGraph's own default (25) was silently in effect here -- neither
# astream_events() call below passed a recursion_limit at all. Confirmed
# via logs/harness.log as the exact, literal cause of every
# "Recursion limit of 25 reached" failure in a dataset/run_dataset.py run:
# several turns were mid-discovery (a wrong guess, then --help, then a
# validation-error-driven pivot to a filter-options lookup) and simply ran
# out of graph steps one or two calls before the correct final call. Not a
# prompt-content fix -- raised so a multi-guess discovery sequence has room
# to actually converge instead of being cut off near the end.
#
# 60, then 120, both still failed live on one particular query ("list
# unread notifications" -- see logs/harness.log thread_id=91b62ab5-...),
# each time after only ~8-9 logged tool calls. That first looked like a
# fixed per-action middleware-overhead multiplier, but isolated replay
# disproved that: instrumenting graph.astream_events() directly (counting
# on_chain_start/on_tool_start events, not just this harness's own
# tool_call_* logging) showed the SAME prompt, run standalone and also
# reproduced as the exact "Hi" -> notifications two-turn sequence that
# failed live, both completing normally in ~20 model rounds / ~40 total
# graph steps -- comfortably under even the original 60. So this is
# run-to-run variance in how many rounds the model needs to converge on
# this manifest-driven CLI's command surface, not a fixed multiplier and
# not an infinite loop: most trajectories are cheap, but an unlucky one
# (extra reasoning-only rounds, or a malformed-tool-call retry caught by
# DeepAgents' own PatchToolCallsMiddleware) can still run well past 120.
# Raised again, further, for headroom over the one observed live failure
# at 120 -- not a guarantee no trajectory ever exceeds this, just a wider
# margin against the variance actually observed.
GRAPH_RECURSION_LIMIT = 200


def _build_agent(access_token: str = "", *, checkpointer: InMemorySaver | None = None):
    """The one place that assembles this harness's DeepAgents graph.

    Called fresh on every `run()`/`stream()` call, exactly like the Claude
    POC's `_build_options` — so a resumed turn can never accidentally skip
    the sandbox or run with a stale token. The backend (and so the
    `execute`/`run_execution_plan` tools bound to it) is constructed new
    each time; the checkpointer is the one piece of state that's
    intentionally shared across calls, since it's what makes session
    continuity possible at all.

    Whether `access_token` actually becomes this call's cybersierra
    identity is conditional, and that's a deliberate fix, not the original
    design: setting `CYBERSIERRA_TOKEN` in the subprocess env
    *unconditionally* — which this did at first — actively breaks the
    "already logged in via `cybersierra auth login-browser`" convenience
    this POC otherwise assumes (see README "Authentication model"),
    because the real CLI checks `process.env.CYBERSIERRA_TOKEN ?? <persisted
    profile>` — JS's `??` only falls back on `null`/`undefined`, not on a
    wrong string, so *any* non-empty value here, including a UI placeholder
    that was never meant to be a real credential, wins over the already-
    authenticated profile and gets rejected by the real backend. So: by
    default, `CYBERSIERRA_TOKEN` is simply never added to this env dict,
    and the persisted profile from the one-time login resolves normally.
    Set `CYBERSIERRA_INJECT_ACCESS_TOKEN=1` to opt into the other, also-real
    mechanism this harness supports — per-request token injection,
    verified in README to override the persisted profile — for anyone
    actually running a shape-B/C multi-tenant demo with real per-user JWTs.

    `access_token` itself is optional now, not required (see
    `server/app.py` and README "Authentication model" — the `no_token` 400
    was dropped): the real cybersierra CLI already holds its own auth in
    `~/.cybersierra/config.json` from that one-time login, so there is no
    per-request credential this POC needs to enforce. The `access_token and`
    guard below matters specifically when `CYBERSIERRA_INJECT_ACCESS_TOKEN=1`
    is set but a caller sends no token: without it, `CYBERSIERRA_TOKEN`
    would be set to `""`, which the CLI's `??` fallback treats as a real
    (empty, rejected) value rather than "absent" — silently breaking the
    persisted-profile fallback the same way the original unconditional-
    injection bug did.
    """
    env = {key: os.environ[key] for key in _PASSTHROUGH_ENV_KEYS if key in os.environ}
    if access_token and os.environ.get(_INJECT_ENV_VAR):
        env["CYBERSIERRA_TOKEN"] = access_token

    backend = AllowlistedShellBackend(
        root_dir=str(PROJECT_ROOT),
        virtual_mode=False,  # real absolute paths, matching skills/ on disk — see README
        env=env,
        inherit_env=False,  # never the ambient os.environ — see module docstring
        redact=access_token,  # scrub from cli_call_* log lines only, see harness/sandbox.py
    )

    return create_deep_agent(
        model=resolve_model(),
        system_prompt=SYSTEM_PROMPT_APPENDIX,
        tools=[make_run_execution_plan_tool(backend)],
        middleware=[ShellSandboxMiddleware(redact=access_token)],
        skills=[str(SKILLS_ROOT)],
        backend=backend,
        checkpointer=checkpointer if checkpointer is not None else _checkpointer,
    )


async def _log_skills_available(graph: Any, config: dict, access_token: str) -> None:
    """Log the full set of skills `SkillsMiddleware` discovered for this
    call — the "what skills is loaded" half of "proof of what's happening
    under the hood," independent of whether the model actually read any of
    them (that's `skill_loaded`, logged by `harness/sandbox.py` when a
    `read_file` call targets a `SKILL.md`).
    """
    try:
        state = await graph.aget_state(config)
    except Exception:  # noqa: BLE001 — logging is best-effort, never fatal to a turn
        logger.debug("skills_available: could not read graph state", exc_info=True)
        return

    skills_metadata = (state.values or {}).get("skills_metadata") or []
    names = [s.get("name") for s in skills_metadata]
    logger.info(
        "skills_available names=%r",
        names,
        extra={"event": "skills_available", "skills": names},
    )
    for error in (state.values or {}).get("skills_load_errors") or []:
        logger.warning(
            "skill_load_error=%r",
            scrub(str(error), access_token),
            extra={"event": "skill_load_error", "error": scrub(str(error), access_token)},
        )


class _LlmCallTracker:
    """Marks each individual model round-trip within a turn — `turn_start`/
    `turn_done` only bracket the WHOLE multi-round turn, which can include
    several LLM calls interleaved with several tool calls, and there was no
    log event for an individual LLM call boundary at all before this. Feeds
    harness/console_format.py's "🧠 LLM" story beat (and its per-turn call
    count); the file log gets these as ordinary `llm_call_start`/
    `llm_call_end` lines like any other event.

    Keyed by the event's own `run_id` rather than a single "last start"
    variable: LangGraph's ReAct-style loop only ever runs one model call at
    a time in this harness today, so a single variable would work too, but
    keying by `run_id` costs almost nothing and stays correct even if that
    ever changes.
    """

    def __init__(self) -> None:
        self._starts: dict[str, float] = {}

    def observe(self, event: dict[str, Any]) -> None:
        kind = event["event"]
        if kind == "on_chat_model_start":
            self._starts[str(event.get("run_id"))] = time.monotonic()
            logger.info("llm_call_start", extra={"event": "llm_call_start"})
        elif kind == "on_chat_model_end":
            start = self._starts.pop(str(event.get("run_id")), None)
            elapsed_ms = round((time.monotonic() - start) * 1000) if start is not None else None
            logger.info(
                "llm_call_end elapsed_ms=%s",
                elapsed_ms,
                extra={"event": "llm_call_end", "elapsed_ms": elapsed_ms},
            )


async def run(prompt: str, access_token: str = "") -> str:
    """Run one turn of the agent for one caller and return only its final
    text. Mirrors the Claude POC's `run()` in shape (single-shot, final-
    text-only), used by simple verify scripts and the CLI demo below.

    access_token is optional, not required (see `_build_agent` and README
    "Authentication model" — the sibling Claude POC's harness required one
    because ITS domain CLI held no identity of its own; the real
    cybersierra CLI is different — it already holds its own auth in a
    persisted profile from a one-time `cybersierra auth login-browser`,
    so this harness has nothing it must reject a call for lacking). Pass
    one anyway to exercise `CYBERSIERRA_INJECT_ACCESS_TOKEN=1` mode.
    """
    graph = _build_agent(access_token)
    thread_id = f"run-{os.getpid()}-{id(graph)}"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": GRAPH_RECURSION_LIMIT}

    logger.info(
        "turn_start thread_id=%s prompt=%r",
        thread_id,
        scrub(prompt, access_token)[:200],
        extra={"event": "turn_start", "thread_id": thread_id, "prompt_preview": scrub(prompt, access_token)[:200]},
    )

    final_text = ""
    llm_tracker = _LlmCallTracker()
    with Netra.start_span("Agent_Turn", as_type=SpanType.TOOL, module_name="agent") as span:
        span.set_attribute("agent.thread_id", thread_id)
        Netra.set_session_id(thread_id)
        if trace_content_enabled():
            # The dashboard's dedicated Input/Output fields, not a generic
            # span attribute — resolves against whatever is currently the
            # root of this trace (see harness/tracing.py's trace_content_enabled
            # docstring for why this needs its own gate independent of
            # Netra.init()'s own trace_content setting).
            Netra.set_root_input(scrub(prompt, access_token))
        try:
            async for event in graph.astream_events(
                {"messages": [HumanMessage(content=prompt)]}, config, version="v2"
            ):
                llm_tracker.observe(event)
                if event["event"] == "on_chat_model_end":
                    message = event["data"]["output"]
                    text = _extract_text(message)
                    if text:
                        final_text = text

            # Only meaningful AFTER the graph has actually run at least once on
            # this thread: SkillsMiddleware populates `skills_metadata` via its
            # `before_agent` hook during execution, not at graph-construction time
            # — calling this before the loop above logged an empty list every
            # time (caught by testing, not assumed; see README "Proper logging").
            await _log_skills_available(graph, config, access_token)

            logger.info(
                "turn_done thread_id=%s",
                thread_id,
                extra={"event": "turn_done", "thread_id": thread_id},
            )
            if trace_content_enabled():
                Netra.set_root_output(scrub(final_text, access_token))
            span.set_attribute("agent.status", "success")
            span.set_success()
        except Exception as exc:  # noqa: BLE001 — re-raised unchanged, span is observability only
            span.set_attribute("agent.status", "failed")
            span.set_error(str(exc))
            raise
    return final_text


# --- streaming, multi-turn variant, used by server/app.py -----------------


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolUseStarted:
    name: str


@dataclass
class Done:
    session_id: str | None
    subtype: str | None
    total_cost_usd: float | None
    usage: dict[str, Any] | None
    num_turns: int | None


@dataclass
class Failed:
    code: str
    message: str


HarnessEvent = TextDelta | ToolUseStarted | Done | Failed


def _extract_text(message: AIMessageChunk) -> str:
    """`AIMessageChunk.content` is either a plain string (most providers) or
    a list of content blocks (Anthropic's native block format). Confirmed
    directly against `langchain_core.messages.AIMessageChunk` and a live
    stream from the installed `langchain-anthropic`/`langchain-openai`
    packages — see README "Confirmed against the installed package."
    """
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "text_delta"):
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


async def stream(
    prompt: str,
    access_token: str = "",
    *,
    session_id: str | None = None,
    resume: str | None = None,
) -> AsyncIterator[HarnessEvent]:
    """Run one turn, yielding incremental events as they arrive.

    Exactly one of session_id (starting a new, server-chosen conversation
    id) or resume (continuing a specific prior one) should be set by the
    caller — matching the Claude POC's `stream()` signature in shape, even
    though DeepAgents' checkpointer doesn't actually distinguish "new" from
    "resumed" the way the Claude SDK does (a `thread_id` with no prior
    checkpoint just starts fresh, silently — no separate call shape, and no
    error if a thread_id has never been seen). See README "Session
    continuity: checkpointer vs on-disk transcript" for what that means in
    practice.

    access_token is optional here too (see `run()`'s docstring for why —
    same reasoning) and, unlike the Claude POC's harness, is not this
    call's identity by default: it only becomes the cybersierra CLI's
    identity when `CYBERSIERRA_INJECT_ACCESS_TOKEN=1` is set (see
    `_build_agent`).
    """
    thread_id = session_id or resume
    if not thread_id:
        raise ValueError("exactly one of session_id or resume is required")

    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": GRAPH_RECURSION_LIMIT}

    logger.info(
        "turn_start thread_id=%s is_new=%s prompt=%r",
        thread_id,
        session_id is not None,
        scrub(prompt, access_token)[:200],
        extra={
            "event": "turn_start",
            "thread_id": thread_id,
            "is_new_session": session_id is not None,
            "prompt_preview": scrub(prompt, access_token)[:200],
        },
    )

    num_turns = 0
    usage_totals: dict[str, int] = {}
    tool_calls_seen: list[str] = []
    llm_tracker = _LlmCallTracker()
    final_text = ""

    try:
        graph = _build_agent(access_token)

        with Netra.start_span("Agent_Turn", as_type=SpanType.TOOL, module_name="agent") as span:
            span.set_attribute("agent.thread_id", thread_id)
            span.set_attribute("agent.is_new_session", str(session_id is not None))
            Netra.set_session_id(thread_id)
            if trace_content_enabled():
                Netra.set_root_input(scrub(prompt, access_token))
            try:
                async for event in graph.astream_events(
                    {"messages": [HumanMessage(content=prompt)]}, config, version="v2"
                ):
                    llm_tracker.observe(event)
                    kind = event["event"]
                    if kind == "on_chat_model_stream":
                        chunk = event["data"]["chunk"]
                        text = _extract_text(chunk)
                        if text:
                            yield TextDelta(text)
                    elif kind == "on_tool_start":
                        tool_calls_seen.append(event["name"])
                        yield ToolUseStarted(event["name"])
                    elif kind == "on_chat_model_end":
                        num_turns += 1
                        message = event["data"]["output"]
                        text = _extract_text(message)
                        if text:
                            final_text = text
                        for key, value in (getattr(message, "usage_metadata", None) or {}).items():
                            if isinstance(value, int):
                                usage_totals[key] = usage_totals.get(key, 0) + value

                # After the run, not before — see the identical comment in run()
                # for why this ordering matters (before_agent hasn't populated
                # skills_metadata until the graph has actually executed once).
                await _log_skills_available(graph, config, access_token)

                logger.info(
                    "turn_done thread_id=%s num_turns=%d tool_calls=%r usage=%r",
                    thread_id,
                    num_turns,
                    tool_calls_seen,
                    usage_totals,
                    extra={
                        "event": "turn_done",
                        "thread_id": thread_id,
                        "num_turns": num_turns,
                        "tool_calls": tool_calls_seen,
                        "usage": usage_totals,
                    },
                )
                span.set_attribute("agent.num_turns", str(num_turns))
                span.set_attribute("agent.tool_calls", json.dumps(tool_calls_seen))
                span.set_attribute("agent.usage", json.dumps(usage_totals))
                if trace_content_enabled():
                    Netra.set_root_output(scrub(final_text, access_token))
                span.set_attribute("agent.status", "success")
                span.set_success()
                yield Done(
                    session_id=thread_id,
                    subtype="success",
                    # LangChain/DeepAgents does not compute a dollar cost the way
                    # the Claude SDK's ResultMessage does (that's Anthropic-specific
                    # pricing knowledge baked into the SDK) — always None here, by
                    # design, not a bug. See README "SSE contract: shape vs content".
                    total_cost_usd=None,
                    usage=usage_totals or None,
                    num_turns=num_turns or None,
                )
            except Exception as exc:  # noqa: BLE001 — mirrors the outer handler below
                span.set_attribute("agent.status", "failed")
                span.set_error(str(exc))
                raise
    except Exception as exc:  # noqa: BLE001 — deliberately broad, see Claude POC's ClaudeSDKError handling
        logger.exception("harness_error thread_id=%s", thread_id, extra={"event": "harness_error", "thread_id": thread_id})
        yield Failed("harness_error", str(exc))


if __name__ == "__main__":
    import asyncio
    import sys

    from dotenv import load_dotenv

    from harness.observability import configure_logging
    from harness.tracing import init_tracing

    load_dotenv(override=False)
    configure_logging()
    init_tracing()

    demo_token = os.environ.get("CYBERSIERRA_DEMO_TOKEN", "placeholder-token-not-real")
    demo_prompt = sys.argv[1] if len(sys.argv) > 1 else "Who are you and what can you help me with?"
    result = asyncio.run(run(demo_prompt, demo_token))
    print(result)
