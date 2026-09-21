# 0005 — CLI manifest extension: what, where, how — mapped to the session-context snapshot

Status: **implemented and verified**, both sides, uncommitted. All 5 write
commands annotated in `agent-manifest.ts` with the exact `requiredPermission`
values in the table below; `types.ts` schema added; `npx tsc --noEmit`
clean, 20/20 backend tests still passing. Harness side (rendering
`user_permissions` into the model's context, plus the two new system-prompt
instructions) implemented too — see the follow-up note at the bottom for
what changed after this was first written, including a correction to the
`0004` demo scenario's failing step.

One thing intentionally **not** done as part of this: getting the manifest
change live in the globally-installed `cybersierra` CLI (that binary is a
standalone npm install, not linked to this checkout — confirmed no `bin`
field for a trivial `npm link`). That rebuild/relink is on you before a
live demo will actually show the new field.

## What `agent-manifest.ts` actually is

Confirmed by reading the file directly (`morpheus_backend/morpheus/src/cli/
manifest/agent-manifest.ts`, its own header comment):

> "THE AGENT API SURFACE. This file is the single, reviewable source of
> truth for what the CLI (and therefore agents) can do. Everything here is
> validated against the live code-derived OpenAPI spec at build time — a
> stale or wrong selector fails the build. Adding a backend route does NOT
> expose it; you must add it here."

It's a deliberately **curated subset**, not a full mirror of the backend —
the file's own comment discloses TPRM alone has ~163 real endpoints; only
a "mostly-read surface plus the common writes" is exposed, with specific
exclusions named directly in the file (file downloads, internal/seeding
routes, an LLM-generated-SQL endpoint excluded on audit-trust grounds).

**Where it lives**: `morpheus_backend/morpheus/src/cli/manifest/
agent-manifest.ts` (328 lines). Its type — `ManifestEntry { method, path,
description?, safe? }`, nested `module → resource → action` — is defined
in the sibling `types.ts`. Only `method`/`path`/`description`/`safe` are
hand-authored here; `params`/`bodyKind` are merged in at build time from
the OpenAPI spec (`morpheus/src/cli/generator/`), confirmed by their
absence from the hand-authored source and presence in the generated
output.

**What actually ships to the agent**: `build-manifest.ts` produces the
real JSON `cybersierra manifest --raw` returns — the thing the harness's
Router step reads to discover commands. Confirmed live: `safe: true`
appears identically in both the TS source and the generated JSON for read
commands; the 5 write commands have **no `safe` field at all** in either
place (omitted, not `false` — confirmed by grep finding zero literal
`safe: false` occurrences anywhere in the file).

## The extension

One new optional field on `ManifestEntry` (`types.ts`):
```ts
requiredPermission?: { obj: string; act: string }[];  // AND semantics
```
Hand-authored per command, same as `safe` already is — confirmed
separately that this **cannot** be derived automatically (neither Fastify's
route table nor the OpenAPI spec exposes the `sub`/`act` values a route's
`verify_permission`/`checkPermission` guard was called with; both are
opaque closures). It flows through the existing build pipeline
automatically once added — no changes needed to the merge logic itself,
same as how `description`/`safe` already pass through untouched.

**Scope for v1** (per the grilling session): the entire manifest has only
**5 write (`safe: false`) commands**, all resolved to a real, unambiguous
permission guard, all in one file:

| CLI command | Method/Path | `requiredPermission` |
|---|---|---|
| `tprm assessments send` | `POST /tprm/assessments/send` | `[{obj:'assessment_template', act:'CREATE'}]` |
| `tprm assessments submit` | `PATCH /tprm/assessments/{id}/submit` | `[{obj:'assessment', act:'CREATE'}]` |
| `tprm assessments update` | `PUT /tprm/assessments/{id}` | `[{obj:'assessment', act:'UPDATE'}]` |
| `tprm assessees create` | `POST /tprm/assessees` | `[{obj:'assessee', act:'CREATE'}]` |
| `tprm risk-score-config update-org` | `PATCH /tprm/risk-score-config/org` | `[{obj:'assessment_risk_score_config', act:'UPDATE'}]` |

## How this maps to the session-context snapshot — the part that actually matters

`0003` already built and verified the other half of this: `SessionEntry.
user_permissions` (deepagent-sdk-cli-poc), a flattened, resource-keyed
dict — `{resource: [action, action, ...]}` — e.g. real data pulled live
for the dev account included `"assessment_template": ["CREATE", "DELETE",
"LIST", "UPDATE", "VIEW"]`.

The shapes are deliberately asymmetric and the comparison is a direct,
one-line-per-pair lookup, not a data-transform:

- Manifest's `requiredPermission` — a **list of `{obj, act}` pairs**
  (supports a command needing more than one distinct permission).
- Session's `user_permissions` — a **dict keyed by `obj`**, each value a
  **list of granted `act`s** for that resource.

The check, for a given command and session:
```
for each {obj, act} in command.requiredPermission:
    if act not in user_permissions.get(obj, []):
        -> this pair is not satisfied, flag it
if all pairs satisfied -> permitted (as far as this check can tell)
```

Worked example, using a real command from the table above and the real
dev-account grants pulled in `0003`:

```
command: tprm assessments send
requiredPermission: [{obj: "assessment_template", act: "CREATE"}]

session.user_permissions["assessment_template"] = ["CREATE","DELETE","LIST","UPDATE","VIEW"]

check: "CREATE" in ["CREATE","DELETE","LIST","UPDATE","VIEW"] -> True -> permitted
```

**Correction to the caveat originally written here**: turned out to be
solvable with the existing admin account after all. Checked live:
`assessment_risk_score_config` (the permission `tprm risk-score-config
update-org` needs) has **zero grantees anywhere in this tenant** — not
`admin`, not any role. So that one command genuinely fails for the exact
account already used all session, no second test identity needed. This
became the corrected failing step for the `0004` demo scenario (its
original "approve assessment" step didn't correspond to any real command
at all) — see `0004`'s update and the implementation note below.

## Follow-up: harness-side implementation (2026-09-16)

Both sides now built and verified, not just designed:

- **Backend**: all 5 rows in the table above are the literal
  `requiredPermission` values now in `agent-manifest.ts`, using the real
  `TPRM_TARGET`/`PERMISSION_ACTIONS` constants (not string literals).
  `types.ts` has the schema. `tsc --noEmit`: clean. 20/20 tests still
  passing (unrelated file, confirms no regression).
- **Harness**: `harness/agent.py` gained `_render_user_permissions()` and
  threads a new `user_permissions` kwarg through `stream()` →
  `_build_agent()` → `system_prompt` (built fresh per call already, so no
  structural change needed there — confirmed no existing param served
  this purpose). `server/app.py`'s two `stream(...)` call sites pass
  `entry.user_permissions` through. Two new paragraphs landed in
  `system_prompt_appendix.md` (Case 1 plan-time flagging, Case 2 reactive
  `exit_code=4` handling — see `0004`), with matching rationale comments
  in `agent.py` per that file's own convention.
- **Confirmed, not assumed**: `skills/cyber-sierra/SKILL.md`'s Planner
  step already dumps a matched operation's *entire* JSON object
  (`print(json.dumps(op, indent=2))`) — a new `requiredPermission` field
  surfaces to the model with zero skill/harness discovery-code changes.
- **Regression-checked**: `verify_server_recent_actions.py` (turns 1-2,
  the actual ring-buffer mechanics) and `verify_server_user_permissions.py`
  (all 4 scenarios) both still pass with the new kwarg threaded through.
- **Still not done**: the CLI rebuild/relink (your own step, by choice)
  needed before the live demo actually shows the new field; nothing
  committed in either repo yet.

## Open items carried over from the grilling session

- **Still blocking, per your lead**: whether "keyword" means this
  `(obj, act)` tuple (what's proposed here, and what the real system
  actually has) or a new composed phrase layer — nothing here should be
  built until that's answered.
- Hand-copy, not automated derivation (confirmed necessary, not just
  preferred).
- Write commands (`safe: false`) first — confirmed to be a small, fully
  resolved set of exactly 5, not an open-ended list.
- Manual PR review for correctness, no dedicated test infra, given the
  scope is 5 entries.
- No `cli:check`-style drift lint yet — deferred fast-follow, not v1.
- Branch: off `v2/wire-tracy`, for now.
