# Architecture changes needed

Split into what's already there and just needs wiring, versus what's
genuinely net-new. See `~/.claude/plans/abstract-growing-lemur.md` for
the fully detailed, file-by-file implementation plan — this is the
summary version.

## Not mode-specific, but bundle in alongside this work: two per-turn efficiency fixes

See `decisions-log.md` for the full reasoning. Concrete changes:

- **`harness/prompts/system_prompt_appendix.md`** — add a paragraph
  telling the model to skip the skill's `npm list -g @cybersierra/
  cybersierra-cli` install check, the same way paragraph 9 already tells
  it to skip the `whoami`-style auth precondition check — the harness's
  own environment setup already guarantees the CLI is installed, so this
  never needs to run per-turn (or arguably ever, from the model's side).
- **`harness/sandbox.py`** — add a module-level `dict` cache (sibling to
  `harness/agent.py`'s `_checkpointer = InMemorySaver()` — must be
  module-level, not an attribute on `AllowlistedShellBackend`, since that
  class is reconstructed fresh every turn by `_build_agent()` and an
  instance-level cache would never survive past one turn). Keyed by exact
  command string; checked in `AllowlistedShellBackend.execute()` before
  `super().execute()` runs; populated on first real execution. Targets
  the Router's Module Inference call specifically (`cybersierra manifest
  --raw | python3 -c "...tree'].keys()..."` — identical string every
  turn, no per-request variables). Process-lifetime only, no TTL —
  consistent with the "no expiry" pattern already settled for sessions.

## Required fix, not deferrable: filesystem-write gating (resolved design)

`ShellSandboxMiddleware._decide`'s mode-aware branches (above) only cover
`execute`/`run_execution_plan`. `write_file`/`edit_file`/`delete` remain
in `_KNOWN_SAFE_TOOLS`, always allowed regardless of mode — which breaks
the "ask/agent_plan never write" guarantee, since skill self-extension
(R4) persists new skills via exactly these tools. Resolved (see
`decisions-log.md`'s "Filesystem writes / skill self-extension" section
for the full reasoning and per-mode table) — must land in the same PR as
the rest of the mode-gating work, not as a follow-up, since it directly
contradicts a guarantee this feature is supposed to provide.

Concrete mechanism:
- **A path check, mirroring an existing pattern.** `ShellSandboxMiddleware`
  already has `_is_skill_read` (a static method detecting "is this
  `read_file` call targeting a `SKILL.md`" by inspecting `args.file_path`)
  — add its write-side counterpart, `_is_skill_write`, checking whether a
  `write_file`/`edit_file`/`delete` call's path targets
  `skills/_generated/`.
- **`ask`**: `write_file`/`edit_file`/`delete` hard-denied unconditionally
  — no exception for `skills/_generated/`, no approval path at all.
- **`agent_plan`**: hard-denied *unless* `_is_skill_write` is true, in
  which case it needs to reach an `interrupt_on` pause — meaning
  `agent_plan` needs `interrupt_on` wired in at all for the first time
  (today only `agent_auto` gets it). A dedicated entry, e.g.
  `interrupt_on={"write_file": InterruptOnConfig(..., when=_is_skill_write)}`,
  separate from `run_execution_plan`'s entry.
- **`agent_auto`**: a *separate* `interrupt_on` entry for skill-writes,
  with its own `when` predicate that does **not** check `write_unlocked`
  — it must always evaluate to "pause," regardless of whether ordinary
  writes are already unlocked in that session. This is a second,
  independent `interrupt_on` key from `run_execution_plan`'s, not a
  shared one — the two need different unlock semantics entirely.

## Prerequisite fix, discovered during investigation

`harness/sandbox.py`'s `ShellSandboxMiddleware._KNOWN_SAFE_TOOLS` does not
include `"run_execution_plan"`, and its `_decide` method denies any tool
call that's neither `"execute"` nor in that set. As written today, this
denies every `run_execution_plan` call outright, in every mode — a
pre-existing gap (`_KNOWN_SAFE_TOOLS` was likely never updated after
`run_execution_plan` was added as a tool), not something introduced by
this work. It needs fixing regardless of mode support, and is the
natural place to add the mode-aware logic below anyway.

## Already-installed primitive, just needs wiring

- **`harness/agent.py`, `_build_agent()`** — add `mode`/`write_unlocked`
  parameters. Thread into `create_deep_agent(...)`: `TodoListMiddleware`
  added only for `agent_plan`; `interrupt_on=...` (the one-time-unlock
  config) added only for `agent_auto`. `_build_agent` is already rebuilt
  fresh per request/turn, so this is a constructor-argument change, not a
  structural one.
- **`harness/sandbox.py`** — `ShellSandboxMiddleware` gets a `mode`
  parameter; its `_decide` method gets two new branches: `run_execution_
  plan` (hard-deny in `ask`/`agent_plan` if any step is `"safe": false`;
  allow through unconditionally in `agent_auto`, letting `interrupt_on`
  handle it) and `execute` (hard-deny outright in `ask`/`agent_plan`;
  unchanged allowlist check in `agent_auto`).

## Genuinely net-new (this is the actual scope driver)

- **A place for `mode` + unlock state to live across turns.**
  `server/sessions.py`'s `SessionEntry` needs `mode` and `write_unlocked`
  fields, plus a `set_mode()` that resets `write_unlocked` whenever a
  session (re-)enters `agent_auto`.
- **A new SSE event type for "paused, awaiting your decision."** This is
  the piece both `executor_tool.py`'s docstring and README.md's "Present
  Plan & Confirm" section name as the actual reason `interrupt_on` wasn't
  wired previously — the current streaming contract only has
  `TextDelta`/`ToolUseStarted`/`Done`/`Failed`. Needs a new variant (e.g.
  `AwaitingApproval`, carrying the pending tool call's command/args) that
  `server/app.py`'s SSE translation and the frontend both need to
  understand.
- **A resume endpoint.** `POST /chat/{session_id}/decide` accepting an
  approve/reject decision, setting `write_unlocked = True` on approve
  *before* resuming, calling `graph.astream_events(Command(resume=
  {"decisions": [...]}), config, version="v2")` against the same
  `thread_id`/`_checkpointer` the original turn used.
- **A second new SSE event type: `ToolUseFinished` (or similar).** Today's
  contract streams `ToolUseStarted` (`{name, args}`) when a tool call
  begins, but nothing when it ends — the frontend has no way to know a
  running command's outcome until the whole turn's `Done`/`Failed`.
  Needed to support a live per-command status map in the UI ("running" →
  "done"/"failed"), which is the concrete frontend UX this is for. Should
  carry at minimum `{name, exit_code, success}` — sourced from the same
  `on_tool_end` event data `harness/agent.py`'s `stream()` already reads
  (today only to extract `actual_outputs_seen` for eval grounding, not
  streamed to the client at all).
- **A third new SSE event type: `PlanStepFinished` — genuinely new
  engineering, not just another dataclass.** Per the pending-action card
  design (`decisions-log.md`), each step of a plan needs to tick off live
  as it completes, not resolve all at once when the whole plan finishes.
  The problem: `run_execution_plan` is one single tool call from the
  graph's point of view — its 4-step loop happens entirely inside one
  Python function (`harness/executor_tool.py`'s `run_execution_plan`),
  invisible to `astream_events`, which only sees the call start and its
  final return value. There is no per-step event today to surface,
  because nothing emits one mid-call.
  Mechanism: LangGraph tools can call `get_stream_writer()` inside their
  own implementation to push custom events *during* execution, which
  `astream_events`/`astream(stream_mode=["custom", ...])` surfaces as
  `on_custom_event`. `run_execution_plan`'s existing per-step loop
  (`executor_tool.py`, already logs `plan_step_start`/`plan_step_done` to
  `harness.log` today) needs one line added per completed step — call the
  stream writer with `{stepId, exit_code, success}` — and `harness/
  agent.py`'s `stream()` needs a new branch handling `on_custom_event`,
  translating each into a `PlanStepFinished` SSE event. This is the one
  piece of the three new event types that needs an actual new streaming
  mechanism wired in, not just a new event shape layered onto data
  already being read.
- **Frontend work in `morpheus_fe`'s Tracy widget** — a mode
  switcher, and a UI for the new "awaiting approval" event (show the
  pending command, Approve/Reject buttons). Outside this repo; needs
  coordination with that team, same as v2's frontend-changes.md
  cross-repo work.

## Required: startup version/internals check, before the server accepts any request

This repo already has an established pattern for exactly this problem:
`eval/netra/run.py`'s `_assert_netra_internals_compatible()` — checks the
exact installed `netra-sdk` version via `importlib.metadata.version`,
`hasattr`-checks the specific private internals it depends on, and calls
`sys.exit()` with a message pointing back at the RCA doc if anything
doesn't match. It runs once, at the top of the entrypoint, before any
real work happens — fails loudly at boot, not confusingly on a live
request.

Our `interrupt_on`/`HumanInTheLoopMiddleware`/`TodoListMiddleware` design
depends just as precisely on specific installed versions behaving exactly
as verified during investigation (`deepagents==0.7.13`,
`langchain==1.3.18`, `langgraph==1.2.11` — see `mode-design.md`'s
"Primitives available" section). A version bump could silently change
`interrupt_on`'s semantics, the `HITLRequest`/`Decision` shapes, or the
middleware-composition order the de-risk check below depends on. This
needs the same treatment, added to `server/app.py`'s startup (before
`uvicorn` starts serving, mirroring how the netra check runs before any
real work in its own entrypoint):

1. Assert the installed `deepagents`/`langchain`/`langgraph` versions
   match what this design was verified against (or at least the same
   major.minor).
2. `hasattr`-check the specific things `_build_agent()` depends on:
   `create_deep_agent` accepting an `interrupt_on` kwarg,
   `HumanInTheLoopMiddleware`/`InterruptOnConfig` importable from
   `langchain.agents.middleware.human_in_the_loop`, `TodoListMiddleware`
   importable from `langchain.agents.middleware`, `Command`/`interrupt`
   importable from `langgraph.types`.
3. Assert `_checkpointer is not None` — `langgraph.types.interrupt()`
   requires one to function at all; cheap to check, confusing to debug
   if it's ever accidentally `None`.

Distinct from the middleware-ordering *behavior* de-risk check below —
this is a fast, static "are the right things present" check that runs
every boot; the ordering check is a heavier scripted test that belongs in
`verify/`, not on every server start.

## De-risk before wiring the endpoints

Middleware ordering (`ShellSandboxMiddleware` vs the auto-appended
`HumanInTheLoopMiddleware`) determines whether a `run_execution_plan`
call with a `safe:false` step actually reaches the interrupt in
`agent_auto` mode — this isn't documented by LangChain/DeepAgents and
needs a small scripted check first, using the same `ScriptedToolCallModel`
pattern `verify/verify_shell_sandbox_denies.py` already uses to force
deterministic tool calls through a real compiled graph.

## Suggested order of implementation

1. Fix the `run_execution_plan` sandbox gap (prerequisite, above) —
   small, independently testable, needed regardless of mode work.
2. Add the startup version/internals check (above) — do this early,
   before writing any code that depends on `interrupt_on`/
   `HumanInTheLoopMiddleware`/`TodoListMiddleware`, so a version mismatch
   is caught immediately rather than discovered mid-implementation.
3. Thread `mode`/`write_unlocked` through `SessionEntry` → `/chat` →
   `stream()` → `_build_agent()`, defaulting everything to today's Auto
   behavior (no behavior change yet — pure plumbing).
4. Add the hard-deny branches to `sandbox.py` for `ask`/`agent_plan`
   (smallest surface, no new SSE contract needed yet).
5. De-risk middleware ordering with a scripted test, then wire
   `interrupt_on` + the new SSE event + the resume endpoint for
   `agent_auto`.
6. Add `TodoListMiddleware` for `agent_plan`.
7. Coordinate with `morpheus_fe` for the mode switcher + approval UI.
