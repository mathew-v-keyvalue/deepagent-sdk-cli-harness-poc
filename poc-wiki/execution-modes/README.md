# Execution Modes: Ask / Agent(Plan, Auto) (proposal)

Status: **implemented on `v3/agent-modes-implementation`** (7 commits:
cleanup, the `run_execution_plan` sandbox fix, the startup dependency
check, mode/write_unlocked plumbing, hard-deny gating, `interrupt_on` +
new SSE events + the `/decide` endpoint, and `TodoListMiddleware`).
`verify/verify_mode_gates.py` (17 checks) and the pre-existing
`verify/verify_shell_sandbox_denies.py` both pass. Not yet pushed to any
remote, no PR opened. Still outstanding: cross-repo coordination
(`morpheus_fe`/`morpheus_backend` — the mode switcher and approval UI live
there, not here), and the two open decisions in `open-questions.md`
(failure-state client messaging, shell-grammar hardening timing).

This folder documents turning the harness's current single mode into a
two-level set of modes, requested by product: something like Cursor's
Ask/Agent/Manual split, or Claude Code's Plan Mode vs default vs
auto-accept.

## Start here

**[decision.md](decision.md)** — the final, standalone proposal and
decision record. Everything below is the working detail behind it; this
is the one document to hand to someone who wasn't in the room.

## Read these in order for the full backing detail

1. **[mode-design.md](mode-design.md)** — what "modes" mean on two other
   platforms (Cursor, Claude Code), and the agreed two-level shape for
   this harness: **Ask** (read-only) vs **Agent**, which itself splits
   into **Plan** (read + produce a plan, never executes) and **Auto**
   (full access, gated once).
2. **[platform-architecture.md](platform-architecture.md)** — one level
   below the UX shape: the actual infra Claude Code and Cursor run this
   on (permission modes, hooks, OS-level sandboxing, allowlists), and
   where each piece maps onto (or is missing from) this harness.
3. **[architecture-changes.md](architecture-changes.md)** — the concrete,
   file-by-file changes needed, split into "already-installed primitive,
   just needs wiring" vs "genuinely net-new."
4. **[open-questions.md](open-questions.md)** — the real risk/effort
   driver (the approval round-trip), cross-repo coordination needed, and
   why the shell sandbox's known prefix-matching gap matters more once
   Auto mode is a named, user-facing mode.
5. **[decisions-log.md](decisions-log.md)** — settled answers from the
   pre-build walkthrough (session state, recent chat, user permission,
   monitoring, partial responses) — what's decided, distinct from what's
   still open above.

## The one-paragraph version

The harness today has exactly one mode: a static shell-command allowlist
(`harness/sandbox.py`) plus a purely conversational "present plan, ask
before running it" instruction to the model — no runtime enforcement.
This was a deliberate, documented scope call (see `harness/
executor_tool.py`'s docstring and README.md's "Present Plan & Confirm"
section), not an oversight: the team already identified DeepAgents'
`interrupt_on` parameter (which wires LangGraph's real graph-pause
primitive) as the right mechanism for a hard approval gate, and
deliberately didn't build it because it needs a new SSE event type
("awaiting approval") the fixed streaming contract doesn't have. The
agreed design has three concrete modes: **Ask** (read-only, no writes,
ever), **Agent → Plan** (read-only plus a structured planning tool;
executing a plan requires switching to Agent → Auto, mirroring how
Claude Code's Plan Mode requires exiting the mode rather than approving
inline), and **Agent → Auto** (full access; the first write-shaped
`run_execution_plan` call in a session pauses once for human approval,
then that session runs unattended for the rest of its life). Ask/Plan
hard-deny writes at the existing sandbox middleware layer; Auto's
one-time gate uses `interrupt_on` — already an installed, unused
dependency — plus a new SSE event type and a resume endpoint, which is
the actual scope driver of this work, not the mode logic itself.
