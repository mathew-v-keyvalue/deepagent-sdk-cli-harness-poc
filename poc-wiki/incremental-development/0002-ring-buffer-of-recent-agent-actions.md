# 0002 — Ring buffer of recent agent actions

Status: **implemented**, verified live, committed (`2a44665`). Full design
plan: `/home/mathewvkariath/.claude/plans/start-with-2-on-fuzzy-quill.md`.

## What changed

- `harness/agent.py`: new `RecentAction` dataclass, new `recent_actions`
  field on `Done`, new `_extract_plan_actions()` extractor
  (`executionLog[].command`/`.success`, same convention as the existing
  `_extract_plan_outputs`), and new population logic in `stream()`'s event
  loop.
- `server/sessions.py`: `SessionEntry.recent_actions`, a
  `deque(maxlen=20)` of `RecentAction` — a small, fixed-capacity, per-session
  record that survives across turns (unlike `stream()`'s own per-turn
  locals, discarded today).
- `server/app.py`: one line in the existing `Done` branch —
  `entry.recent_actions.extend(event.recent_actions)`.
- `verify/verify_server_recent_actions.py`: new verify script.

## Why

Motivating gap (from `poc-wiki/execution-modes/decisions-log.md`'s
"Failure states" section): when a multi-step plan halts on a `forbidden`
result partway through, be able to say "here's what already succeeded"
without re-deriving it from raw checkpoint history.

## A real bug found via live verification, not just code reading

The original design (both the approved plan and two independent Plan-agent
investigations that fed it) assumed `on_tool_end`/`on_tool_error` on
`astream_events` would see every `execute` call, success or failure,
including sandbox denials — reasoning that `AllowlistedShellBackend.execute()`
returns a normal `ExecuteResponse(exit_code=126)` for a denial, which would
flow through the tool machinery like any other result.

**That's wrong for the enforcement layer that actually matters.**
`ShellSandboxMiddleware.awrap_tool_call` (`harness/sandbox.py`) intercepts
and returns its own `ToolMessage` *before* the `execute` tool's own traced
Runnable ever executes — confirmed live, by instrumenting a real
`astream_events` run against a scripted, deterministic tool call
(`cybersierra auth set-token ...`, denied by the middleware's
`DENIED_COMMAND_PREFIXES` group-deny): **no `on_tool_start`, `on_tool_end`,
or `on_tool_error` fires at all** for a middleware-denied call. The first
implementation shipped, compiled clean, passed unit-level checks — and
silently recorded nothing for the exact scenario (a denied action) the
whole feature exists to report. Caught only because the plan's own
verification step was run for real instead of trusted on paper.

The fix: `on_chain_end` with `event["name"] == "tools"` is the one event
that reliably fires for *both* a denied call and a real one — its
`data["input"]` is the batch of requested tool_calls (with `id`),
`data["output"]["messages"]` is the resulting `ToolMessage`s, correlated by
`tool_call_id`. Population logic was rewritten around that event instead;
the pre-existing `on_tool_end`-based `actual_commands_seen`/
`actual_outputs_seen` tracking (used for Netra eval grounding, not this
feature) was left completely untouched.

Also **dropped** the `on_tool_error` branch the original diff added:
reasoned through that even if it fires correctly, that data can never reach
`SessionEntry` in the current design anyway — it only flows out of `stream()`
via `Done`, and a turn that hits a genuine tool exception propagates to
`stream()`'s outer handler and yields `Failed` instead. Kept it out rather
than ship speculative code with no currently-observable effect.

## Verified

- `python3 -m py_compile` on all four touched files.
- Direct unit checks of `RecentAction`/`Done`/`_extract_plan_actions`
  against realistic and malformed `executionLog` payloads.
- `verify/verify_server_recent_actions.py`, run live against the real
  server (in-process via `httpx.ASGITransport`, not `_server_helper.py`'s
  subprocess-based `running_server()` — that would make
  `server.sessions.store` a different process's object, so any assertion
  on it would be hollow) with a `ScriptedToolCallModel` (same pattern as
  `verify_shell_sandbox_denies.py`) rather than a live model — first
  attempt used a live model instructed to run the denied command directly;
  it read `SKILL.md`, recognized the command as disallowed, and simply
  refused in text without ever calling `execute` — exactly the "vacuous
  test" failure mode that script's own docstring warns about. Scripting the
  tool call removed that nondeterminism while still exercising the real
  `harness/agent.py`/`server/app.py`/`harness/sandbox.py` code. **PASSES.**
- Also live-confirmed the "real call, ran, nonzero exit" branch (as opposed
  to the denial branch) using an allowed command against the (currently
  unreachable) local backend — correctly produced `success=False` via the
  `artifact.get("exit_code")` path, not the `status == "error"` path.

## Follow-up: the disclosed `success=True` gap is now closed

The original writeup disclosed that `success=True` had never been exercised
live, on the reasoning that it needed a reachable `cybersierra` backend
(`CYBERSIERRA_BASE_URL=http://localhost:8080`, not running here). That
premise was too narrow. The code under test only ever reads
`artifact["exit_code"]` — it cannot tell which binary produced the `0`. And
`_CYBERSIERRA_COMMAND_PATTERN` is a `search`, not a `match`, so the
cybersierra-shaped substring it extracts need not be the command itself.

So `verify/verify_server_recent_actions.py` gained a second turn using
`python3` (already an `ALLOWED_COMMAND_PREFIXES` entry, and already used
this way by `verify_server_multi_session_isolation.py`) printing a
cybersierra-shaped string: allowed by both enforcement layers, really run as
a subprocess, exits 0, produces a real `{"exit_code": 0}` artifact. **PASSES
live** — `RecentAction(action='vendor risk list', success=True, ...)`.
Nothing in the mechanism under test is stubbed; only the model's tool-call
decision is, as before. What it still does not prove: anything about the
real `cybersierra` CLI's behavior against a live backend — but that was
never what this code path does.

The same change made the two turns share one `session_id`, which closed a
second, previously unnoticed gap: `SessionEntry.recent_actions`' whole
reason for existing is surviving across turns, and the original script only
ever ran one turn. Turn 1's denied action is now asserted to still be
present after turn 2. Also **PASSES**.

## Not verified (known gap, disclosed rather than assumed)

- `run_execution_plan`'s per-step extraction (`_extract_plan_actions`) was
  unit-tested directly but not exercised through a live turn — per existing
  comments in `harness/agent.py`, the model calls `execute` directly in
  practice, not `run_execution_plan`, so this path is real but rarely hit.

## Impact

`SessionEntry.recent_actions` now exists and is populated correctly —
verified live for the success case, both kinds of failure case, and
survival across turns.
Not exposed over the SSE contract or any endpoint yet — deliberately
deferred, per the approved plan. TTL eviction, chat summarization, and the
Postgres write-behind flush remain separate, unstarted backlog items.
