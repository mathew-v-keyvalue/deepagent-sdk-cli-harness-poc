# Open questions and risks

## RESOLVED: filesystem-write gating — see `decisions-log.md`

Was flagged "must address before build" — now decided. Skill-writing
(`write_file`/`edit_file`/`delete` targeting `skills/_generated/`) gets
its own always-ask treatment, distinct from and stricter than ordinary
CLI writes; everything else stays hard-denied in `ask`/`agent_plan`. Full
reasoning and the per-mode table are in `decisions-log.md`'s "Filesystem
writes / skill self-extension" section.

## The approval round-trip is the real effort, not the sandboxing

Everything about deciding "is this call allowed in this mode" is already
centralized and cheap to parameterize (`sandbox.py`). The actual new work
is the pause/resume protocol: a new SSE event type, a resume endpoint,
and a frontend that understands both. This was scoped out previously for
exactly this reason (see README.md's "Present Plan & Confirm" section) —
it's a real, non-trivial addition to the streaming contract, not a
config change.

## User permission: not ours to enforce, but "forbidden" needs its own failure state

Per-user authorization (can this specific user actually do X) is already
handled downstream — the real cybersierra backend enforces it on every
call via the forwarded JWT, independent of anything this harness does.
We don't need to duplicate that decision.

What we *don't* have is a pre-flight version of it. Checked directly:
`cybersierra manifest` (`skills/cyber-sierra/_internal/shared/manifest-
usage.md`) returns a static catalog of every command, tagged `safe: true/
false` (read/write) — the same output for every caller regardless of
token. It is not scoped to what the calling user is actually entitled to
do. So a plan can be built entirely out of commands that exist and look
safe on paper, and still fail mid-execution because this particular user
isn't allowed to run one of them.

The system already half-anticipates this: `harness/executor_tool.py`'s
`EXIT_CODE_MEANING` table already has `4: "forbidden"` as a distinct,
named outcome — but today it's handled exactly like any other failure:
the plan halts on the first non-zero exit code, no special messaging, no
"here's what already succeeded before we hit this."

Two genuinely separate follow-ups, not one:
1. **A real pre-flight entitlements check** would need a new API from the
   backend team (a "what can this token actually do" endpoint) — this
   isn't something we can build from our side; worth raising with them as
   a separate ask, not scoped into this work.
2. **Treating `forbidden` (exit_code=4) as its own distinct failure
   state** — buildable now, independent of #1: when a plan halts on a
   forbidden step, tell the user specifically *that's* why (not a generic
   error), and surface whatever earlier steps in the same plan already
   completed successfully, instead of just reporting failure. This is
   part of the "failure states" design work, not the permission-gating
   work.

## Should ad-hoc `execute` also be gated in Agent → Auto?

The current design deliberately scopes the one-time approval gate to
`run_execution_plan` only, leaving ad-hoc `execute` calls unchanged in
`agent_auto` (today's behavior). "Present Plan & Confirm" — the thing
being hardened — is specifically about execution plans, and `sandbox.py`
has no read/write classifier for a raw ad-hoc command the way plan steps
have a `safe` field. If ad-hoc `execute` turns out to be a meaningful
write path in practice, this is the natural next tightening, but it
would need a new command-level read/write heuristic to be built first.

## The shell sandbox's known gap matters more once Auto mode is explicit

`sandbox.py`'s allow/deny check is prefix-only, not shell-grammar-aware —
a shared, documented gap with the sibling POC (e.g. `cybersierra
manifest; rm -rf /` isn't caught by prefix matching alone). Once "Agent →
Auto" becomes a named, user-selectable mode where a session runs
unattended after one approval, that prefix-matching gap is a bigger part
of the actual safety boundary for that mode than it was when the whole
harness had only one undifferentiated mode. Worth deciding whether Auto
mode shipping is the trigger to finally fix shell-grammar parsing.

## Does Agent → Plan need `TodoListMiddleware`, or is text enough for v1?

`TodoListMiddleware` gives a real data artifact (structured todos) the
frontend can render distinctly from chat text — probably what product
wants for a Cursor/Claude-Code-like plan view. But it's an extra moving
part. If the timeline is tight, Plan mode could ship v1 as "read-only
tool allowlist + the model presents its plan as ordinary text" —
functionally correct, without the todo-list UI — with `TodoListMiddleware`
as a fast-follow once the approval round-trip is proven out in Auto mode.

## Netra/eval impact

Not investigated here: `eval/netra/` calls `harness.agent.run()`
in-process using the current single-mode assumption. Adding modes likely
means the eval harness needs a `mode` axis too, and any new "awaiting
approval" pause needs a decision about whether a paused turn counts as
success, failure, or a new outcome category for scoring. Worth a pass
once the mode plumbing itself is decided.

## Cross-repo coordination needed before starting

Mode switching and the approval UI are user-facing surfaces that live in
`morpheus_fe`'s Tracy widget, and the new SSE event type needs handling on
the `morpheus_backend` `/tracy/chat` proxy side too. Recommend a short
design check-in with those two teams before locking the SSE contract
shape, the same way v2's wiring work did.
