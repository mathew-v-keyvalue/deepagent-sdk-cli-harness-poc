# Architecture changes needed

Split into what's already there and just needs wiring, versus what's
genuinely net-new. See `~/.claude/plans/abstract-growing-lemur.md` for
the fully detailed, file-by-file implementation plan — this is the
summary version.

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
- **Frontend work in `morpheus_fe`'s Tracy widget** — a mode
  switcher, and a UI for the new "awaiting approval" event (show the
  pending command, Approve/Reject buttons). Outside this repo; needs
  coordination with that team, same as v2's frontend-changes.md
  cross-repo work.

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
2. Thread `mode`/`write_unlocked` through `SessionEntry` → `/chat` →
   `stream()` → `_build_agent()`, defaulting everything to today's Auto
   behavior (no behavior change yet — pure plumbing).
3. Add the hard-deny branches to `sandbox.py` for `ask`/`agent_plan`
   (smallest surface, no new SSE contract needed yet).
4. De-risk middleware ordering with a scripted test, then wire
   `interrupt_on` + the new SSE event + the resume endpoint for
   `agent_auto`.
5. Add `TodoListMiddleware` for `agent_plan`.
6. Coordinate with `morpheus_fe` for the mode switcher + approval UI.
