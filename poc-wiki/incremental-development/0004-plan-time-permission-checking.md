# 0004 — Plan-time permission checking: system prompt + two test cases

Status: **design + test plan only, nothing implemented.** Sequenced after
the CLI manifest extension (`0003`'s handoff note) — this is what to build
once that lands, and how to prove it works, including the case where it
doesn't (yet) apply.

## Goal

Once the manifest carries `requiredPermission` per command (`0003`'s
handoff) and the harness has a session's resolved `user_permissions`
(`0003`, already built but not yet rendered anywhere), the next piece is
using both **at plan-generation time** — before a single command runs —
so the model can flag a step that will fail on permission grounds instead
of finding out mid-execution.

Two genuinely different situations follow from this, and both need to work
well, not just the happy path:

- **Case 1 — checked, caught early.** The step's command has a
  `requiredPermission` annotated in the manifest, the session has
  `user_permissions`, they don't match → the model flags it in the plan it
  presents, before running anything.
- **Case 2 — not checked, fails mid-plan.** Either the command isn't
  annotated yet (true for all 37 commands today — the manifest extension
  hasn't started), or `user_permissions` isn't available for some reason →
  nothing catches it in advance, the plan runs, and the real backend
  rejects the step with `exit_code=4` (forbidden) partway through.

**Important framing for the team**: Case 2 isn't a bug to eliminate, it's
the permanent fallback state for every command until it's individually
annotated — and even after full annotation, a stale cache or a mid-session
permission change could still produce it. Both paths need to degrade well;
Case 1 is a nice-to-have layered on top of Case 2 working correctly, not a
replacement for it.

## The system prompt addition (Case 1)

New instruction, same voice as the existing auth-failure instruction in
`harness/prompts/system_prompt_appendix.md`:

> When building a plan, check each step's command against the permission
> requirements available to you (if the harness has provided them for that
> command). If a step needs a permission you don't have for this session,
> don't silently include it as if it will succeed — flag it explicitly in
> the plan you present, in plain language (name what's missing, e.g.
> "approving assessments" — never the raw internal permission key or exit
> code), and ask whether to proceed with the rest of the plan or skip that
> step. If a command's permission requirement is not available to you at
> all, present it normally — an unannotated command is not a known
> failure, just an unverified one; don't claim a false guarantee either
> way.

This only fires when the harness actually supplies per-command permission
requirements alongside the plan-building step — which depends on the
manifest extension existing and on rendering `user_permissions` into
context (`0003`'s still-open item), neither built yet.

## The reactive instruction (Case 2 — also not yet implemented)

Already drafted in this same design conversation (see prior discussion of
`exit_code=4`), reproduced here since it's the other half of this
deliverable:

> If a plan step fails with `exit_code=4` (forbidden), don't treat it as a
> generic error. Tell the user: (1) which steps before it already
> completed, by name; (2) that this step failed because their account
> lacks the needed permission, in plain terms; (3) don't attempt anything
> after it; (4) offer to continue without it, or note who'd need to handle
> it.

Worth being explicit with the team: `SessionEntry.recent_actions` (built,
`0002`) already has the "what succeeded" data this needs. The
`exit_code=4`-gets-special-treatment behavior itself is not built — today
a forbidden step is handled like any other generic failure, per
`poc-wiki/execution-modes/decisions-log.md`'s own disclosure.

## Test scenario (both cases, same underlying plan)

A 5-step TPRM workflow, using real domain shapes confirmed live earlier:

| Step | Command | Permission needed |
|---|---|---|
| 1 | List vendors due for assessment | `assessment: VIEW/LIST` |
| 2 | List available assessment templates | `assessment_template: VIEW/LIST` |
| 3 | Create the assessment | `assessment: CREATE` |
| 4 | **Approve the assessment for sending** | `assessment: APPROVE` |
| 5 | Confirm delivery via activity log | `activity-logs: VIEW/LIST` |

Test user's real resolved grants (from `0003`'s live pull) include full
CRUD on `assessment` but not `APPROVE` — step 4 is the natural gap,
matching the real data already gathered rather than an invented one.

### Test prompt — Case 1 (requires: step 4's command manually annotated with `requiredPermission` in a dev copy of the manifest, plus `user_permissions` present in context)

> "I need to send a new risk assessment to our vendors that are due, using
> the standard template — create it and get it approved so it goes out
> today."

**Expected**: the presented plan explicitly flags step 4 before anything
runs — something like *"Step 4 (approve the assessment) needs approval
permission on assessments, which this account doesn't have — want me to
run steps 1-3 and leave it ready for someone with approval access?"* — not
discovered by actually attempting it.

### Test prompt — Case 2 (requires nothing extra — this is today's default state for every command, since none are annotated yet)

Same prompt, run as-is right now, with no manifest changes:

> "I need to send a new risk assessment to our vendors that are due, using
> the standard template — create it and get it approved so it goes out
> today."

**Expected** (once the reactive instruction above is built): steps 1-3
genuinely run and succeed, step 4 gets a real `exit_code=4` from the
backend, and the response reads like *"I ran the first 3 steps
successfully: [names them]. Step 4 (approving the assessment) failed —
this account doesn't have permission to approve assessments. I didn't
attempt step 5, since it depends on approval completing."* — not a
generic "something went wrong."

**Note the identical prompt for both cases is deliberate**: it's the exact
same user request; what changes is only whether the specific command
involved happens to be annotated in the manifest yet. That's the honest
demo of "this degrades gracefully whether or not manifest coverage is
complete."

## What's needed before either test can actually run

- **Case 1**: the manifest schema change (`0003`'s handoff) with at least
  one real command (ideally the step-4 equivalent) annotated; rendering
  `user_permissions` into the model's context (open item from `0003`);
  the new plan-time system prompt instruction above.
- **Case 2**: only the reactive instruction above — `recent_actions`
  already exists and already has the right data; this is purely a system
  prompt + response-shaping change, no new data plumbing needed.

Neither is built yet. This document is the spec + test plan to hand to
the team, not a completed feature.
