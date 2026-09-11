# Open questions and risks

## The approval round-trip is the real effort, not the sandboxing

Everything about deciding "is this call allowed in this mode" is already
centralized and cheap to parameterize (`sandbox.py`). The actual new work
is the pause/resume protocol: a new SSE event type, a resume endpoint,
and a frontend that understands both. This was scoped out previously for
exactly this reason (see README.md's "Present Plan & Confirm" section) —
it's a real, non-trivial addition to the streaming contract, not a
config change.

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
