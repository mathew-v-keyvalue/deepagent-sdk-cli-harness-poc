# Decisions log

Settled answers from the pre-build walkthrough, covering every keyword
the eng lead raised: agent execution modes, session state, recent chat,
user permission, monitoring, partial responses, SSE stream events, and
failure states. Each entry includes a concrete example — see
`open-questions.md` for what's still unresolved rather than decided.

## Agent execution modes

Ask / Agent → Plan / Agent → Auto — see `mode-design.md` for the full
design. Same request, three modes, to make the difference concrete:

You type: **"Turn off vendor Acme Corp."**

- **Ask**: "I can't make changes in this mode — I can tell you Acme Corp's
  current status if that helps." No plan, no write attempt, ever.
- **Agent → Plan**: "Here's the plan: 1) Deactivate Acme Corp
  (`safe: false`). Switch to Agent Auto mode to run this." Produces the
  plan as a structured artifact, stops — does not run it.
- **Agent → Auto**: "Sure, I'll turn off Acme Corp now" — then pauses
  before actually deactivating it, waiting for one approval (first write
  in the session). Approve once, and the *next* vendor you ask it to
  deactivate in the same session runs with no further pause.

## Filesystem writes / skill self-extension: always ask, stricter than a normal write

Resolved after a dedicated walkthrough — was flagged "must address before
build" in `open-questions.md`, now decided. The problem: `write_file`/
`edit_file`/`delete` are always allowed today, in every mode, with no
gating at all — and these are exactly the tools the harness's skill
self-extension mechanism (R4/"Reflection") uses to persist a brand-new
skill to disk (`skills/_generated/<name>/SKILL.md` + `metadata.json`).

The reasoning: writing a new skill is more permanent and harder to walk
back than a single CLI action (like deactivating a vendor), so it gets
its own, stricter rule — not just folded into the same one-time-unlock
mechanism as ordinary writes.

| Mode | Ordinary writes (`execute`/`run_execution_plan`) | Writing a new skill (`skills/_generated/*`) |
|---|---|---|
| Ask | Hard denied | Hard denied — no exception, no approval path |
| Agent → Plan | Hard denied | **Pauses for approval, right there in Plan mode** — no need to switch to Auto first |
| Agent → Auto | First write pauses once, then unattended | **Always pauses for approval, every single time** — never inherits the one-time-unlock, regardless of whether ordinary writes are already unlocked in that session |

**Example**: a session has been unlocked for an hour — it's been
deactivating vendors all afternoon with zero pauses. The model decides
this looks like a repeatable pattern worth learning as a skill. That
specific action still pauses for approval, even though every other write
in that same session hasn't required one in an hour. Skill-writing is
deliberately exempt from the unlock state.

Mechanically, this needs the same kind of path-based tool-call inspection
`ShellSandboxMiddleware` already has a precedent for — its existing
`_is_skill_read` static method already detects "is this `read_file` call
targeting a `SKILL.md`" by inspecting the call's `file_path` argument.
The new check is the write-side mirror of that: "does this `write_file`/
`edit_file`/`delete` call's path target `skills/_generated/`," gating
`agent_plan`'s and `agent_auto`'s treatment of it accordingly, using a
dedicated `interrupt_on` entry (see `architecture-changes.md`) rather
than the same `when` predicate that governs `run_execution_plan`.

## Session state: no expiry

Sessions live until the process restarts, same as today — no new
inactivity-timeout mechanism. `write_unlocked` only resets on an explicit
mode switch into `agent_auto`, not on a timer.

**Example**: you approve a write at 10am, keep chatting on and off, and
at 4pm you ask it to deactivate a second vendor — same session, still
unlocked, still no pause, even though hours passed. The only thing that
re-locks it is explicitly switching the session's mode away from
`agent_auto` and back.

## Recent chat: no persistence needed, reload = fresh start, regardless of mode

Confirmed this doesn't change with modes: today's checkpointer-backed,
in-memory-only chat history (lost on reload/restart) is fine as-is.

**Example**: message 1 — "What's Acme Corp's status?" → "Active." Message
2 — "Deactivate it." The agent resolves "it" from message 1's context via
the in-memory checkpointer. If you refresh the browser tab between the
two messages, "it" means nothing anymore — the new session has no memory
of Acme Corp ever being mentioned. Same behavior in every mode; not a gap
this work needs to close.

## User permission: not ours to enforce; `forbidden` needs its own failure state

Per-user authorization is already handled downstream by the real
cybersierra backend via the forwarded JWT — this harness doesn't
duplicate that decision. `cybersierra manifest` is a static, non-user-
scoped catalog (confirmed directly), so there's no existing pre-flight
"what can this user actually do" check available to us.

**Example**: a support-tier user's token has no delete permission. They
ask the agent to deactivate 3 vendors. The plan looks completely fine on
paper — `safe: false` just means "this is a write," it says nothing about
*this* user's entitlement. Steps 1 and 2 succeed; step 3 comes back
`exit_code=4` ("forbidden") because the real backend rejected it. Today
that's treated like any other failure — plan halts, generic error. Two
separate follow-ups: (1) a real pre-flight entitlements API is new
backend-team scope, not ours to build; (2) treating `forbidden`
specifically — "you don't have permission for this one, but the first two
succeeded" — is buildable now, independent of (1).

## Monitoring: no active alerting for now, but new events must still be logged

Passive visibility (Netra traces + `harness.log`) is sufficient for this
phase — no paging/alerting on rejection rate, pause duration, etc. New
mode events still need their own log lines regardless:
`approval_paused`, `approval_decided`, `mode_switched`.

**Example**: if rejection rate quietly climbs to 80% today, nobody gets
paged — that was the explicit decision. But because `approval_decided`
gets logged every time either way, someone can go grep `harness.log` or
query Netra next week and actually find that pattern, instead of it being
invisible entirely. The tradeoff is "no active alerting," not "no
visibility."

## Partial responses: lock the session, show a full step breakdown, tick off live, reword the prompt

**Example** (the one that made this concrete): you ask to deactivate 3
stale vendors. The agent streams "Here's my plan:" — that reads like it's
about to just happen. But it's a write, so it's actually paused, waiting
on approval — nothing has run yet. Consensus reached, four parts:

1. **Already structurally enforced**: `/chat` returns `409` while a
   decision is pending — no new message can interrupt it.
2. **Frontend shows the full step breakdown, not just a headline** —
   every step in the plan, each tagged Read or Write (from that step's
   `safe` field), so the approval is fully informed:
   ```
   ⏸ Waiting for your approval

     1. Look up vendor status for Acme, Beta, Gamma      [Read]
     2. Deactivate Acme Corp                              [Write]
     3. Deactivate Beta Supplies                          [Write]
     4. Deactivate Gamma Logistics                        [Write]

     [ Reject ]                              [ Approve all ]
   ```
   One decision for the whole plan (not per-step — per-step approval is a
   possible v2 extension, not v1).
3. **Each step ticks off live as it actually completes** — ✓/✗ per line,
   in real time, not one "running..." blob that resolves all at once when
   the whole plan finishes. See the SSE section below for what this
   requires.
4. **On reject, the agent follows up naturally** — this isn't extra
   plumbing: a rejection becomes a normal tool result fed back to the
   model in the same graph run, so it already generates a next response
   on its own (e.g. "No problem — want me to just do Acme Corp instead?").
   Only needs a one-line system-prompt nudge for tone, not new mechanism.
5. **Prompt-level**: reword the announcement itself — "I'd like to
   deactivate these — approve to proceed" instead of "I'll do this now,"
   so the text doesn't read as already-completed.

Same tradeoff as "recent chat" above: reloading while a decision is
pending loses that pending state too — not a new gap.

## SSE stream events

Today's contract: `TextDelta`, `ToolUseStarted`, `Done`, `Failed`. Three
additions from this work: `AwaitingApproval`, `ToolUseFinished`, and
**`PlanStepFinished`** (see `architecture-changes.md` for the last one's
mechanism — it's the one genuinely new engineering task among the three,
not just a new dataclass).

**Example** — the actual event sequence for the 3-vendor plan in
Agent → Auto, first write of the session:
```
TextDelta("Here's my plan: ...")
ToolUseStarted({name: "run_execution_plan", args: {plan_json: "..."}})
AwaitingApproval({action_requests: [{name: "run_execution_plan", args: {...}}]})
   ...(client sends the human's decision to /chat/{id}/decide)...
PlanStepFinished({stepId: 1, exit_code: 0, success: true})   // Acme Corp status looked up
PlanStepFinished({stepId: 2, exit_code: 0, success: true})   // Acme Corp deactivated
PlanStepFinished({stepId: 3, exit_code: 0, success: true})   // Beta Supplies deactivated
PlanStepFinished({stepId: 4, exit_code: 0, success: true})   // Gamma Logistics deactivated
ToolUseFinished({name: "run_execution_plan", exit_code: 0, success: true})
TextDelta("Done — all 3 vendors are now inactive.")
Done({session_id, ...})
```
Without `ToolUseFinished`, the frontend never learns the outcome of the
whole `run_execution_plan` call until the turn's `Done`. Without
`PlanStepFinished`, it never learns about any *individual* step either —
`run_execution_plan` is one single tool call from the graph's point of
view; the 4-step loop happens entirely inside that one Python function
(`harness/executor_tool.py`), invisible to today's event stream, which
only sees "the tool call started" / "the tool call ended."

## Efficiency: two redundant CLI calls found during this walkthrough

Not mode-specific, but surfaced while walking through per-turn behavior —
both are candidates to fix alongside this work since they affect every
single turn regardless of mode.

**1. `npm list -g @cybersierra/cybersierra-cli` (the install check)**
`SKILL.md` line 42 says this check "must happen at the start of every
session" — i.e. once per session, not once per message. Nothing enforces
that today; it's a sentence in a markdown file the model is trusted to
remember. The harness already fixed the identical shape of problem for a
different check: the system prompt (`harness/prompts/
system_prompt_appendix.md`, paragraph 9) explicitly tells the model to
skip a skill-prescribed `whoami`-style precondition check that would
otherwise fire before every action, because the harness already resolves
that deterministically before the model's turn starts. **There's no
equivalent override for the install check.** Fix: add the same kind of
system-prompt instruction — the harness can already guarantee the CLI is
installed (it's part of this server's own environment setup, not
per-request), so tell the model to skip this check entirely rather than
trying to get it to only run it "once per session" through prose alone.

**Example**: without the fix, every single message potentially re-runs
`npm list -g` before doing anything else — one extra subprocess call and
round-trip per turn, for a fact that never changes between messages on a
running server.

**2. `cybersierra manifest --raw | python3 -c "...tree'].keys()..."` (Module Inference)**
This one *is* supposed to run every turn (Router step 2, `SKILL.md` line
110-118) — every new message could be about a different topic, so
figuring out relevant modules fresh each time is correct behavior, not a
bug. But the manifest itself is tied to CLI version, not to the user or
the session — it's the same static catalog for everyone until someone
explicitly reinstalls the CLI. That makes it cacheable even though the
*call* is intentionally per-turn.

**The gotcha**: `AllowlistedShellBackend` (where the real subprocess call
happens, `harness/sandbox.py`) is rebuilt fresh on every single turn —
`_build_agent()` constructs a new instance every time. A cache stored as
an *instance* attribute would be wiped every turn and never actually
cache anything. It has to be a **module-level** cache (same pattern
`harness/agent.py` already uses for `_checkpointer = InMemorySaver()` —
one object, shared across every request, for the life of the process).

**The plan**: a module-level dict in `harness/sandbox.py`, keyed by the
exact command string, checked in `AllowlistedShellBackend.execute()`
before `super().execute()` runs. This targets the Module Inference call
specifically — it's the exact same string, byte-for-byte, every turn (no
per-request variables), so exact-string caching gets a 100% hit rate
after the first turn, with no TTL/expiry logic needed (process-lifetime
only, same "no expiry" pattern already settled for sessions — the
manifest only changes on an explicit CLI reinstall, which doesn't happen
automatically). The Planner's separate filtered fetch (`modules =
['MODULE_1', 'MODULE_2']`, varies per request) won't reliably hit this
same cache since its command string changes — handling that one too
would mean caching the underlying raw manifest data and re-running just
the `python3` filter locally, which is more invasive; not needed for the
primary win.

**Example**: turn 1 pays the real subprocess cost once; every turn after
that, for the rest of the process's life, gets the module list instantly
from memory instead of shelling out again.

## Failure states

The concrete list for this system, each with its own example:

- **Recursion limit hit** — the model needs more discovery rounds than
  the graph's step budget allows (documented, has happened live with
  multi-guess CLI discovery). Turn ends in `Failed`, generic message.
- **Sandbox denial** — model tries `cybersierra auth login`; denied
  outright, `exit_code=126`, turn continues (denial is just a tool
  result, not a turn-ending failure) — the model sees the denial message
  and has to recover or explain it to the user.
- **Forbidden mid-plan** — covered above under "user permission": step 3
  of 3 fails with `exit_code=4` after steps 1-2 already ran.
- **Rejected approval** — user clicks Reject on the Acme Corp pause;
  nothing runs, session stays locked (not unlocked), the *next* write
  attempt pauses again from scratch.

Each of these currently surfaces to the client as one generic `Failed`/
error shape — the open item (see `open-questions.md`) is whether the
client needs to distinguish between them (e.g. "hit a limit" vs "you
don't have permission" vs "denied by policy" are very different messages
to show a user) or whether one generic failure display is fine for now.
