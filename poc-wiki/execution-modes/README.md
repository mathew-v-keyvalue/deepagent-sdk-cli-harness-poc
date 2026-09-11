# Execution Modes: Ask / Agent(Plan, Auto) (proposal)

Status: **proposal — approved design, implementation not started.** This
work is scoped to a separate branch, picked up later — nothing in
`v2/eval-harness` should change for this yet. This folder plus
`~/.claude/plans/abstract-growing-lemur.md` (the implementation plan) are
the handoff record for whoever starts that branch.

This folder documents turning the harness's current single mode into a
two-level set of modes, requested by product: something like Cursor's
Ask/Agent/Manual split, or Claude Code's Plan Mode vs default vs
auto-accept.

## Read these in order

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
