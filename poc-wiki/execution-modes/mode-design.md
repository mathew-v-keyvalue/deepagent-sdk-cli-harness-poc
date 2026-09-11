# Mode design: what the other platforms do, and the agreed shape here

## What "modes" mean elsewhere

**Cursor** splits interaction into three modes: **Ask** (conversational,
read-only, no edits), **Agent** (autonomous multi-file edits + terminal
commands — the default), and **Manual** (targeted, explicitly-scoped
edits the user reviews one at a time). Separately, "Auto-Run" (formerly
"Yolo mode") is a natural-language allowlist that lets Agent mode run
commands without per-command confirmation. Cursor also has an unrelated
"Auto" *model-selection* mode (routes a request to whichever underlying
LLM fits cost/quality) — worth keeping distinct from the permission-style
"auto" this doc is about.

**Claude Code** splits along a similar axis: **Plan Mode** (tool access
restricted to read-only exploration; the agent must present a plan and
get explicit approval — by exiting the mode — before any mutating tool
call is allowed), **default mode** (each not-pre-allowlisted tool call
prompts for per-call approval), and **auto-accept-edits / bypass-
permissions mode** (tool calls run without per-call prompts, gated only
by a static allow/deny list).

## The agreed shape for this harness: Ask, and Agent(Plan | Auto)

Not three flat, parallel modes — a two-level split:

- **Ask** — read-only. Answers questions using read-only tools/CLI calls
  only (e.g. "how many pending assessments"). Never proposes or runs a
  write, and never produces a "plan" artifact either — just an answer.
- **Agent → Plan** — same read-only tool access as Ask, plus a structured
  planning tool (`write_todos`) so the model's output is an explicit plan
  artifact instead of prose. It never executes writes itself. Getting a
  plan to actually run requires the user switching the session to
  Agent → Auto — there is no "approve inline while staying in Plan mode"
  path, deliberately mirroring Claude Code's Plan Mode (you exit the mode
  to unlock execution; you don't approve your way into an edit while
  still in it).
- **Agent → Auto** — full tool access, matching today's behavior. The
  first write-shaped call in a session — specifically, the first
  `run_execution_plan` call containing a step marked `"safe": false` —
  pauses for a **one-time** human approval. Once approved, that session
  is "unlocked": further write attempts in the same session proceed
  without further prompts. Rejecting a write does **not** unlock the
  session — the next write attempt pauses again. This is a genuine,
  hard runtime upgrade over today's purely conversational "Present Plan &
  Confirm" instruction — not a per-call gate that asks every single time.

## Primitives available to build this, already installed and unused

Confirmed by reading the installed packages directly
(`deepagents==0.7.13`, `langchain==1.3.18`, `langgraph==1.2.11`):

- **`interrupt_on`** — a parameter on `deepagents.create_deep_agent()`
  (`deepagents/graph.py`). When set, it attaches LangChain's
  `HumanInTheLoopMiddleware`, whose `after_model` hook inspects the
  model's pending tool calls and, for any matching `interrupt_on` entry
  (optionally gated by a `when` predicate), calls
  `langgraph.types.interrupt(...)` — a real graph-pause, distinct from
  the session checkpointer, resumable via
  `graph.astream(Command(resume={"decisions": [...]}), config)` against
  the same `thread_id`. This is exactly the mechanism `harness/
  executor_tool.py`'s docstring and README.md's "Present Plan & Confirm"
  section already name as "the right primitive," not built only because
  of the missing SSE event type. Here, `when` is keyed off a per-session
  `write_unlocked` flag rather than firing on every call — giving the
  one-time-unlock behavior Agent → Auto needs, not a per-call gate.
- **`TodoListMiddleware`** — ships in `langchain.agents.middleware.todo`,
  exports a `write_todos` tool and `PlanningState.todos`. Not imported
  anywhere in `deepagents/graph.py` today. This is the building block for
  **Agent → Plan**: gives the model an explicit, structured planning tool
  instead of relying on prose, so "present the plan" has an actual data
  artifact the UI can render, distinct from ordinary chat text.
- **`ShellSandboxMiddleware`/`is_command_allowed`** (`harness/sandbox.py`)
  — already the single, centralized per-call allow/deny decision point
  for every shell call, used identically by both the sync and async tool
  paths. This is where Ask/Plan's *hard* deny of writes lives — a
  separate, simpler mechanism from Auto's one-time `interrupt_on` pause;
  it either denies a call outright or lets it through unchanged, and
  never pauses one itself.

Net effect: none of the three modes require restructuring the agent loop
or replacing the sandbox's enforcement point. They require threading a
`mode` value through to decisions that are already centralized, and
finally building the approval round-trip that was scoped out on purpose
until now.
