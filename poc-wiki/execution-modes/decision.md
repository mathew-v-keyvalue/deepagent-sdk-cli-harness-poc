# Execution Modes — Proposal & Decisions

**Status:** Implemented, on `v3/agent-modes-implementation` (branched off
`feat/agent-modes-explorations`, itself off `v2/eval-harness`) — 7 commits,
`verify/verify_mode_gates.py` (17 checks) and the pre-existing sandbox
verify script both passing. Not pushed to any remote yet, no PR opened.
This document is the final, standalone record — see `decisions-log.md`
for the fuller reasoning/examples behind each item and
`architecture-changes.md` for file-by-file implementation detail (now
"as-built" rather than purely prospective).

## 1. Problem

The harness currently has one undifferentiated mode: every session gets
identical, full capability, gated only by a static shell-command
allowlist. "Present Plan & Confirm" — the intended safety step before any
write action — exists only as a sentence in the system prompt. Nothing in
code enforces it; the model can call a write-shaped action without ever
asking. This matches the real production system's own design (also
unenforced), but it's a gap worth closing now that the harness is moving
toward broader use.

## 2. Proposed design: Ask, and Agent → {Plan, Auto}

Not three flat, parallel modes — a two-level split, session-scoped (set
once per session, not per message):

| Mode | Read access | Write access |
|---|---|---|
| **Ask** | Yes | None. No plan, no write, ever. |
| **Agent → Plan** | Yes | Produces a structured plan, never executes it. Running a plan requires switching to Agent → Auto. |
| **Agent → Auto** | Yes | Full access. The first write-shaped action in a session pauses once for approval; once approved, the rest of the session runs unattended. |

This mirrors Claude Code's own Plan Mode / default / auto-accept split
and Cursor's Ask / Agent / Manual split, adapted to this harness's
existing primitives (`ShellSandboxMiddleware` for hard denials,
`interrupt_on` — an already-installed, previously-unused LangGraph
primitive — for the approval pause).

## 3. Decisions

### 3.1 Filesystem writes / skill self-extension (resolved — was a hard blocker)

`write_file`/`edit_file`/`delete` are DeepAgents' filesystem tools, used
by the harness's skill self-extension mechanism to persist a new skill
to `skills/_generated/`. These were not covered by any mode gating until
this decision — meaning even Ask mode could write a new skill file. Now:

| Mode | Ordinary filesystem writes | Writing a new skill (`skills/_generated/*`) |
|---|---|---|
| Ask | Hard denied | Hard denied — no exception |
| Agent → Plan | Hard denied | Pauses for approval, directly in Plan mode |
| Agent → Auto | Hard denied outside this path | Always pauses for approval — every time, independent of whether ordinary writes are already unlocked |

Skill-writing is treated as more permanent/impactful than an ordinary
write, so it never inherits Auto's one-time-unlock — it asks every time,
in every mode where it's possible at all.

### 3.2 Session state

Sessions live until the process restarts — no new expiry/timeout
mechanism. The one-time write-unlock resets only on an explicit mode
switch into Agent → Auto, not on a timer.

### 3.3 Recent chat

No change from today: in-memory-only conversation history, lost on
reload/restart, identical behavior in every mode.

### 3.4 User permission

Per-user authorization is enforced downstream by the real backend via the
forwarded token — this harness does not duplicate that decision, and
there is no existing pre-flight "what can this user do" API to check
against before planning. A plan can look valid and still fail mid-way
with a `forbidden` result once it hits a step this specific user isn't
entitled to run. Decision: treat `forbidden` as its own distinct failure
state (see 3.8), rather than a generic error. A true pre-flight
entitlements check is out of scope — it would require new backend-team
work, not something buildable from this side.

### 3.5 Monitoring

No active alerting for this phase (no paging on rejection rate, pause
duration, etc.). Passive visibility is sufficient — but every new mode
event still needs its own log line, with the same discipline as existing
`cli_call_start`/`tool_call_denied` events: `approval_paused`,
`approval_decided`, `mode_switched`.

### 3.6 Partial responses

A turn can pause mid-stream, after text has already been shown to the
user that reads like a completed action ("I'll deactivate these vendors
now") when nothing has actually run yet. Handling:

- The session is locked (`409` on any new message) until the pending
  decision is resolved.
- The UI shows the full plan breakdown while pending — every step, each
  tagged read/write — not just a one-line summary, so approval is fully
  informed. One decision covers the whole plan (not per-step, for v1).
- Each step ticks off live as it actually completes, rather than
  resolving all at once when the whole plan finishes.
- On rejection, the agent follows up naturally in the same turn (this is
  the existing mechanism's default behavior, not new plumbing — a
  rejected decision is fed back as a normal tool result) — just needs a
  system-prompt nudge for tone.
- The model's own phrasing is adjusted to propose rather than assert
  ("I'd like to do X — approve to proceed" instead of "I'll do X now").

### 3.7 SSE stream events

Three additions to today's `TextDelta`/`ToolUseStarted`/`Done`/`Failed`
contract:
- `AwaitingApproval` — a turn has paused, pending a decision.
- `ToolUseFinished` — a tool call's outcome, not just its start (today's
  contract has no "finished" signal at all for an in-progress call).
- `PlanStepFinished` — per-step progress *inside* a single
  `run_execution_plan` call. This is the one genuinely new engineering
  task among the three: `run_execution_plan`'s step loop runs entirely
  inside one Python function, invisible to the event stream today, and
  needs LangGraph's `get_stream_writer()` called from inside that loop to
  surface each step as it completes.

### 3.8 Failure states

Four concrete cases this system can end a turn on: hitting the graph's
recursion/step limit, a sandbox denial (disallowed command), a
`forbidden` result mid-plan (this user lacks permission for one step),
and a rejected approval. All four currently surface as one generic
failure shape to the client. **Open**: whether these need distinct
client-facing messaging (a limit vs. a permission problem vs. a policy
denial are different things to tell a user) — not yet decided, see
Section 5.

### 3.9 Efficiency fixes found during this review

Two redundant per-turn CLI calls, unrelated to mode gating but bundled
into the same work:
- The skill's CLI-install check (`npm list -g ...`) is meant to run once
  per session but has no enforcement — fix via a system-prompt
  instruction to skip it, mirroring an identical fix already made for a
  different precondition check.
- The Router's per-turn manifest fetch is correctly called every turn,
  but its content is static per CLI version — cacheable at the module
  level (must be module-level, not instance-level, since the shell
  backend is rebuilt fresh every turn).

## 4. Architecture changes required (summary)

Full file-by-file detail lives in `architecture-changes.md` and the
implementation plan. In brief: `harness/sandbox.py` gains mode-aware
gating (including the new skill-write path check); `harness/agent.py`
gains `mode`/`write_unlocked` parameters and two new `interrupt_on`
entries (`run_execution_plan`, and a separate one for skill-writes);
`server/sessions.py` and `server/app.py` gain the session-level mode
field and a new `/chat/{id}/decide` resume endpoint; a startup
version/internals check is added for the LangGraph/LangChain primitives
this depends on, mirroring the existing `netra-sdk` compatibility
assertion pattern already in this repo.

## 5. Open items still requiring a decision

- Should ad-hoc `execute` calls also be gated in Agent → Auto, or is
  scoping the pause to `run_execution_plan` only correct for v1?
- Is the shell sandbox's prefix-matching gap (not shell-grammar-aware)
  worth hardening now, or a deliberate fast-follow after modes ship?
- Does Agent → Plan need the structured planning tool (`write_todos`) for
  v1, or is plain-text plan output enough to start?
- Do the four failure states (3.8) need distinct client-facing messages,
  or is one generic failure display acceptable for now?
- Cross-repo coordination with `morpheus_fe`/`morpheus_backend` (mode
  switcher UI, approval UI, SSE contract changes) has not started.
- Netra/eval impact of adding modes has not been investigated.

## 6. Suggested implementation order

1. Fix the pre-existing `run_execution_plan` sandbox gap (unrelated
   prerequisite — it's currently denied outright in every mode).
2. Add the startup version/internals check.
3. Thread `mode`/`write_unlocked` through session → endpoint → stream →
   agent build, with no behavior change yet (pure plumbing).
4. Add hard-deny branches for Ask/Agent → Plan, including the skill-write
   path check.
5. De-risk middleware ordering, then wire `interrupt_on` (both entries),
   the new SSE events, and the resume endpoint.
6. Add the planning tool for Agent → Plan.
7. Coordinate with `morpheus_fe` for the mode switcher and approval UI.
