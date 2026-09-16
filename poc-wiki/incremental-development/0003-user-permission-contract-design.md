# 0003 — User permission contract: design + CLI manifest handoff

Status: **implemented and verified on both sides**
(`morpheus_backend`'s Casbin resolution + `deepagent-sdk-cli-poc`'s
consumption), uncommitted in both repos pending review. `tsc --noEmit`
clean and 20/20 Jest tests passing on the backend; 4/4 scenarios passing
in a new `verify/verify_server_user_permissions.py` on the harness side,
plus a clean regression pass of the pre-existing `recent_actions` ring
buffer. Only the CLI manifest annotation (`requiredPermission` per
command) remains a separate, unstarted handoff — see that section below,
still belongs in its own `morpheus_backend` session.

## Context

Backlog item from the session-context work: give the harness a way to know
what a user is actually allowed to do, so it can (a) reduce how often a
multi-step plan hits the `forbidden` failure state
(`poc-wiki/execution-modes/decisions-log.md`'s "Failure states" section),
and (b) eventually support advisory context for planning. Originally
explored via the CLI's own `permissions policies list` command; the actual
direction (from a conversation with the eng lead) is different and better:
the backend already has Casbin RBAC — extend the chat API to send a
resolved permission set, and extend the CLI manifest so each command
declares what it needs.

## What was found, live, not assumed

- `cybersierra permissions policies list --subject <anything>` **does not
  filter server-side** — confirmed live: identical response regardless of
  the `--subject` value. It returns the tenant's entire raw Casbin store:
  Casbin model config + all `p` (policy) rows + all `g` (role-grouping)
  rows (205 rows total in this tenant: 186 `p` / 19 `g`, ~35 roles). The
  caller has to do the role-inheritance graph-walk themselves to get one
  user's actual grants — this is exactly what the backend's own
  `getImplicitPermissionsForUser` (`morpheus/src/modules/super_search/
  services/search.service.ts:65`) already does server-side. Strong
  supporting evidence for why the backend should resolve and send this,
  not the CLI/harness.
- Real Casbin rows are `(sub=role, obj=resource, act=regex-pattern)`
  triples — e.g. `["p", "auditor", "policy", "(VIEW)|(LIST)|(DOWNLOAD)"]`
  — **not** free-text phrases like `"create assessment"`. Confirmed both
  by reading `morpheus/src/plugins/permissions/constants.ts` and live,
  against the real running backend. If "create assessment"-style keywords
  are still the intent, that's a new composed-phrase layer that doesn't
  exist anywhere in the codebase today — worth confirming with the lead
  before the manifest work starts (see handoff note below).
- **A real data quirk to defend against**: one live `g` row is
  `["g", "user", "(ACKNOWLEDGE)"]` — a grouping row whose target isn't a
  real role name, apparently a shortcut for granting the `ACKNOWLEDGE`
  action directly rather than via a `p` row. A naive graph-walk (see
  below) will pick this up as if `"(ACKNOWLEDGE)"` were an inherited role.
  Not fixed here (not ours to fix — it's backend seed data); any consumer
  of this data needs to tolerate it.
- Real end-to-end example: resolved for the dev account
  (`mathew.v@keyvalue.systems`, roles `admin`+`user` directly, 12 more
  inherited) — 55 distinct resources, e.g. `assessment: [CREATE, DELETE,
  LIST, UPDATE, VIEW]`, `RISK: [CREATE, DELETE, DOWNLOAD, LIST, UPDATE,
  VIEW]`. Full resolved set available on request; not reproduced in full
  here since it's tenant/account-specific and large.

## Decisions reached

0. **Target architecture from the start, not a harness-side interim.**
   Considered and rejected: the harness calling `permissions policies list`
   itself (via the CLI) and doing the role-graph-walk in Python
   (`resolve_user_permissions` below) at session start. Technically
   workable and fully within this repo's control, but explicitly rejected
   in favor of building the real target the first time — RBAC resolution
   is an authorization concern this codebase already treats as backend-
   owned everywhere else (`open-questions.md`: *"the real backend
   enforces [authorization]... we don't need to duplicate that
   decision"*). A harness-side reimplementation would be a second,
   independent copy of the backend's authorization model with a real
   drift risk (if Casbin's matcher/policy semantics ever change, e.g. an
   explicit `deny` rule gets introduced, the harness's copy keeps
   producing the old answer silently) — plus it turns out to duplicate
   work `getImplicitPermissionsForUser` already does for free server-side
   (see below). `resolve_user_permissions` is kept in this doc as a
   reference/verification tool (it's what proved the real data shape live)
   — not something that gets wired into `server/sessions.py`.
1. **Advisory context, not an enforcement gate.** The harness never blocks
   a step itself based on its own cached copy — that stays the real
   backend/Casbin call's job. This only reduces how often a doomed plan
   gets attempted; matches what's already on record in `poc-wiki/
   execution-modes/decision.md` §3.4, and avoids duplicating a real
   authorization decision client-side (staleness risk: cached permissions
   vs. a mid-session role change).
2. **Contract shape drops `roles`.** Only the resolved `subject` + flattened
   `grants` (resource → deduped action list) matter for the advisory
   comparison — the model needs "can I CREATE on `assessment`," never
   "the user is an `admin`." Role names are an intermediate artifact of the
   graph-walk, not something worth persisting or injecting into context.
3. **Manifest annotation is `(obj, act)` tuples**, mirroring the codebase's
   own existing `verify_permission({sub, act})`/`checkPermission([sub,
   act])` pattern — not a new free-text vocabulary — pending the lead's
   confirmation noted above.
4. **AND semantics, fail-closed.** A command may require zero, one, or
   several `(obj, act)` pairs (all required). A command with no declared
   requirement yet is treated as unverifiable, not as implicitly safe —
   matches every other enforcement point in this harness already being
   deny-by-default (`ShellSandboxMiddleware`'s allowlist, the `auth`
   group-deny).
5. **Delivery cadence — now has a concrete recommendation, still needs the
   lead's sign-off.** Not "once per session vs. every call" as a binary
   choice after all: `morpheus_backend` already has a Redis-backed cache
   plugin (`fastify.cache`, `morpheus/src/plugins/cache/cache.plugin.ts`)
   with a ready-made `withCache<T>(key, operation, ttl, prefix)`
   get-or-compute-and-set helper, already used for exactly this shape of
   need (per-user, rarely-changing, TTL'd data —
   `morpheus/src/modules/cli_auth/services/cli_auth_session.service.ts`).
   Recommendation: resolve permissions on every `/tracy/chat` call as
   before, but wrap the resolution in `withCache(userId, ..., ttl, ...)` —
   cheap on a cache hit (the common case, since permissions rarely
   change), self-bounding staleness on a miss (no separate invalidation
   logic needed), no session-lifecycle bookkeeping required on either
   side. Avoids repeating this repo's own documented mistake (the
   unbounded per-turn manifest refetch, `decisions-log.md`'s efficiency
   section) without inventing new cache infrastructure.

## Addendum: Tracy session-id minting moved server-side (implemented)

A separate but adjacent decision, made and **implemented** in `morpheus_backend`
(not just designed) on the same `/tracy/chat` call path this doc already
touches — recorded here so anyone picking up the permission-contract work
below sees the current, real shape of `tracy_chat.ts`/`tracy_chat.service.ts`
before reading the "illustrative sketch" section further down (that sketch
predates this change and its confirmed line numbers have shifted).

**Problem:** `sessionId` used to be client-minted and required
(`dtos/tracy.dto.ts`'s old comment: "the frontend mints this before ever
sending the first message"). Morpheus never validated it — any authenticated
user could pass any `sessionId` and `POST /tracy/chat` would forward it to
deepagent unchecked, so nothing stopped user A from supplying user B's
session and riding into their conversation.

**Decision — Option B (lazy, server-minted):**
- `sessionId` is now optional on `TracyChatBodyDto`. Omit it on the first
  message of a new conversation.
- The backend mints one server-side (`crypto.randomUUID()`), scopes it to
  `{userId, tenantId}` in Redis (`RedisCache`, key `tracy_session:<id>`, 24h
  sliding TTL — refreshed on every turn, no separate invalidation needed;
  same style as `cli_auth_session.service.ts`, **not** a new Postgres table —
  keeps `tracy_chat.ts`'s existing "stateless by design, no
  ai_conversations/ai_messages row" property for conversation *content*
  intact), and returns it to the client via the **`X-Tracy-Session-Id`
  response header** (chosen over an SSE frame or a separate `POST
  /tracy/sessions` endpoint — fewer round trips, and the frontend already has
  to read response headers for the SSE fetch).
- On later turns the client sends that `sessionId` back; the backend looks up
  the Redis record and rejects with `403 ForbiddenError` (before any SSE
  headers are sent — a plain JSON error, not an SSE `error` frame) if it's
  missing or owned by a different `userId`/`tenantId`.
- CORS: `Access-Control-Expose-Headers: X-Tracy-Session-Id` is set alongside
  the existing manual `Access-Control-Allow-Origin` block, otherwise browser
  JS can't read a non-simple response header cross-origin.

**Implemented, TDD, all green:**
- `src/modules/tracy/services/tracy_session.service.ts` (new) —
  `resolveTracySession(cache, sessionId, {userId, tenantId})`.
- `src/modules/tracy/dtos/tracy.dto.ts` — `sessionId` now `Type.Optional`.
- `src/modules/tracy/services/tracy_chat.service.ts` — `streamTracyChat` now
  takes the resolved `sessionId` as an explicit parameter instead of reading
  `body.sessionId`, so an unresolved/unvalidated id can never reach deepagent.
- `src/modules/tracy/handlers/tracy_chat.ts` — resolves the session before
  any SSE setup; sets `X-Tracy-Session-Id` + the CORS expose header.
- `src/errors/bad_request.error.ts` — added `forbiddenError()`, mirroring
  `invalidToKenError()`, used in the route's `response.403` schema doc.
- Tests: `test/modules/tracy/services/tracy_session.service.test.ts`,
  `test/modules/tracy/handlers/tracy_chat.test.ts` (13 tests, all passing).

**Not done / still open:** the CLI/harness side of this repo still needs to
handle a `403`/ownership-rejected response on `/chat` calls if it ever
resumes a stale or foreign `session_id` (e.g. surfacing it distinctly from
`session_expired`) — not investigated here, flagged for whoever picks up the
harness-side work next.

## Backend implementation scope (`morpheus_backend`)

**The short version** — why there's no walk to write:

| | Raw CLI (`permissions policies list`) | `getImplicitPermissionsForUser` (what gets called instead) |
|---|---|---|
| Scope | Entire tenant, unfiltered, ignores `--subject` | Just this one user, already role-resolved |
| Role-walk needed on our side? | Yes | **No** — Casbin does it internally |
| `act` still a raw regex string? | Yes | Yes — still needs the normalize step |

So: no walk to write, one small normalize step to reuse (already exists,
`search.service.ts:67-85`), three small additive changes to wire it
through. Code below is an **illustrative sketch**, not a verified exact
diff — grounded in confirmed file:line quotes where noted, but the full
surrounding file wasn't read line-by-line; verify signatures against the
live file before implementing.

### 1. `tracy_chat.ts`'s handler — pass the enforcer + cache through

Confirmed current call site, `tracy_chat.ts:72`:
```ts
await streamTracyChat(req.body, req.authData, requestId, traceHeaders);
```
Change — `this.casbin`/`this.cache` are already reachable (handler is
`this: FastifyInstance`), just unused today:
```ts
await streamTracyChat(req.body, req.authData, requestId, traceHeaders, this.casbin, this.cache);
```

### 2. `streamTracyChat` — resolve, cache, normalize, attach

Confirmed current form body, `tracy_chat.service.ts:73-92`:
```ts
const form = new URLSearchParams({
  message: body.message,
  session_id: body.sessionId,
  access_token: authData.token,
});
```
Addition — a small helper plus one new form field:
```ts
async function resolvePermissions(userId: string, casbin: Enforcer, cache: RedisCache) {
  return cache.withCache(
    userId,
    async () => {
      const rows = await casbin.getImplicitPermissionsForUser(userId); // [[obj, act], ...] — already role-resolved
      const grants: Record<string, Set<string>> = {};
      for (const [obj, act] of rows) {
        const actions = act.match(/\(([\w-]+)\)/g)?.map((a) => a.slice(1, -1)) ?? [];
        actions.forEach((a) => (grants[obj] ??= new Set()).add(a));
      }
      return Object.fromEntries(Object.entries(grants).map(([k, v]) => [k, [...v].sort()]));
    },
    300, // ttl seconds — real number pending the lead's sign-off, decision #5
    'tracy-chat-permissions',
  );
}

// inside streamTracyChat, before building `form`:
const permissions = await resolvePermissions(authData.userId, casbin, cache);
const form = new URLSearchParams({
  message: body.message,
  session_id: body.sessionId,
  access_token: authData.token,
  permissions: JSON.stringify(permissions),
});
```
`dashboard_chat.service.ts:56-71` is the closest existing precedent for
attaching derived context to one of these proxy calls (there,
`tenantId`/`userId` as headers) — no existing proxy attaches permission
data specifically, so this is new but idiomatically consistent.

### 3. `server/app.py`'s `/chat` handler (this repo) — consume it

Not started, depends on 1 and 2 existing first. Current signature,
`server/app.py:137-147`:
```python
@app.post("/chat", dependencies=[Depends(_verify_service_auth)])
async def chat(
    message: str = Form(...),
    session_id: str | None = Form(None),
    access_token: str = Form(""),
):
```
Addition — one new optional field, parsed onto `SessionEntry`:
```python
async def chat(
    message: str = Form(...),
    session_id: str | None = Form(None),
    access_token: str = Form(""),
    permissions: str | None = Form(None),  # JSON string: {resource: [actions]}
):
    ...
    if permissions:
        entry.user_permissions = json.loads(permissions)
```

## The normalization function

Validated against the real live response shown above — this is the exact
logic used, cleaned up as a reusable reference. **Not wired into this
repo's code yet** (see "What's blocked") — this is scaffolding to lift in
once the real chat-API contract exists, not a live code path today.

```python
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class UserPermissions:
    """The advisory-context shape for one user's resolved permissions.
    Deliberately excludes `roles` — see decision #2 above: only the
    flattened resource -> action-list grants matter for the comparison
    this exists to support; role names are a resolution intermediate, not
    something worth persisting or injecting into the model's context.
    """

    subject: str
    grants: dict[str, list[str]]
    fetched_at: float


def resolve_user_permissions(
    subject: str,
    p_rows: list[tuple[str, str, str, str]],
    g_rows: list[tuple[str, str, str]],
    fetched_at: float,
) -> UserPermissions:
    """Walk Casbin's role-inheritance graph and flatten policy rows into
    one user's actual grants.

    `p_rows`/`g_rows` are the raw Casbin policy/grouping rows this repo's
    `cybersierra permissions policies list` command returns today (see
    module docstring: it returns the ENTIRE tenant's store unfiltered, not
    scoped to `--subject` — this function is what does the filtering the
    CLI doesn't). If/when the backend chat-API extension lands a
    pre-resolved permission set instead, this function becomes unnecessary
    for the harness's own use once the backend sends a pre-resolved
    permission set instead (the chosen target architecture, see decision
    #0 above) — kept here purely as a reference/verification tool: this is
    the exact logic that proved the real data shape live against the raw
    CLI endpoint. The backend does NOT need an equivalent of this
    function's role-walk loop — `enforcer.getImplicitPermissionsForUser`
    already does that resolution internally as a library call.

    Known data quirk (confirmed live, not this function's bug): a `g` row
    can target something that isn't a real role name (e.g.
    `("g", "user", "(ACKNOWLEDGE)")` — a modeling shortcut, not a role).
    This function doesn't special-case it; the resulting phantom "role"
    only matters if something later tries to resolve grants FOR it (it
    won't match any real `p.sub`, so it's inert here, just noise if you
    inspect the intermediate role set directly).
    """
    roles = {subject}
    changed = True
    while changed:
        changed = False
        for _, sub, obj in g_rows:
            if sub in roles and obj not in roles:
                roles.add(obj)
                changed = True
    roles.discard(subject)

    grants: dict[str, set[str]] = {}
    for _, sub, obj, act in p_rows:
        if sub not in roles:
            continue
        # Casbin's act field is a regex-alternation string, e.g.
        # "(VIEW)|(LIST)|(CREATE)" — not a delimited list. Extract each
        # parenthesized action name individually.
        actions = re.findall(r"\((\w[\w-]*)\)", act)
        grants.setdefault(obj, set()).update(actions)

    return UserPermissions(
        subject=subject,
        grants={resource: sorted(actions) for resource, actions in sorted(grants.items())},
        fetched_at=fetched_at,
    )
```

## CLI manifest handoff note (for a separate `morpheus_backend` session)

This piece lives in a different repo (`morpheus/src/cli/manifest/`) and
belongs in its own session there, not this thread. Everything needed to
start it:

- **Schema addition** — `morpheus/src/cli/manifest/types.ts:11-19`,
  `ManifestEntry` gains one new optional field:
  ```ts
  export interface ManifestEntry {
    method: HttpMethod;
    path: string;
    description?: string;
    safe?: boolean;
    requiredPermission?: { obj: string; act: string }[];  // AND semantics
  }
  ```
- **Annotation convention** — mirror the pattern already used for every
  real route in this backend (`verify_permission({sub, act})` /
  `checkPermission([sub, act])`, e.g. `morpheus/src/modules/registry/
  routes/index.ts:56-60`), reusing the existing `TPRM_TARGET`/
  `GOVERNANCE_TARGET`/`PERMISSION_ACTIONS`-style constants
  (`morpheus/src/plugins/permissions/constants.ts`) — not a new
  free-text vocabulary.
- **One open question to resolve before annotating anything**: confirm
  with the lead whether "keyword" meant this `(obj, act)` tuple (which is
  what actually exists in the codebase) or a new composed human-readable
  phrase layer — this determines the field's actual value type.
- **Fail-closed default**: any manifest entry with no `requiredPermission`
  declared should be treated by consumers as unverifiable, not as
  implicitly safe — 37 commands exist today and none are annotated yet
  (confirmed: no field, comment, or TODO anywhere in `agent-manifest.ts`
  or `types.ts` gestures at this).

## Implementation record

Both sides built and verified, uncommitted:

**`morpheus_backend`** (branch `v2/wire-tracy`):
- New `src/modules/tracy/services/tracy_permissions.service.ts` —
  `resolveTracyPermissions(cache, casbin, userId)`, exactly as scoped
  above.
- `tracy_chat.ts` — resolves permissions (own try/catch, advisory-only
  degrade), passes through to `streamTracyChat`.
- `tracy_chat.service.ts` — 6th optional param, attached to `form` when
  present.
- New `test/modules/tracy/services/tracy_permissions.service.test.ts` (6
  tests: merge/dedup, resource separation, empty-action-token guard,
  missing-obj/act guard, `withCache` call shape, error propagation) and
  updates to `test/modules/tracy/handlers/tracy_chat.test.ts` (2 new/
  changed tests: the 6th arg is passed through; a resolution failure
  degrades without an SSE error). `npx tsc --noEmit --strict false`: 0
  errors. `npx jest test/modules/tracy`: 20/20 passing.

**`deepagent-sdk-cli-poc`**:
- `server/sessions.py` — `SessionEntry.user_permissions: dict[str,
  list[str]] | None = None`.
- `server/app.py` — new `permissions: str | None = Form(None)`, parsed
  onto `entry.user_permissions` at the top of `event_source()` (refreshed
  every turn, not just new sessions — matches decision #5), with a
  `json.JSONDecodeError` guard that logs and leaves it unset rather than
  500ing.
- New `verify/verify_server_user_permissions.py` (4 scenarios: exact
  storage, omitted-stays-`None`, malformed-doesn't-500, second-turn
  overwrite-not-merge) — all passing. Regression-checked against
  `verify/verify_server_recent_actions.py`: its turns 1-2 (the actual
  ring-buffer mechanics) still pass unaffected.

**One unrelated bug surfaced, not caused by this work**: `verify_server_
recent_actions.py`'s turn 3 (a live `run_execution_plan` call, previously
always skipped — no reachable backend when it was written) now fails: the
tool is denied by `ShellSandboxMiddleware` as "unrecognized" — it was
never added to that middleware's allow-path at all. Confirmed via
`git diff` that nothing in this session's work touches `harness/sandbox.py`
or any file in that call path. Flagged as its own follow-up task rather
than fixed here (out of scope for this doc's work, and the right fix needs
checking whether `AllowlistedShellBackend` already re-validates each plan
step's command independently before a middleware-level allowlist change
would be safe).

Not done, still genuinely blocked/parked:
- The manifest actually declaring `requiredPermission` per command
  (separate `morpheus_backend` session, per the handoff note above).
- The lead's sign-off on: (a) the caching recommendation/TTL value
  (decision #5), and (b) whether "keyword" meant the real `(obj, act)`
  tuple or a new phrase layer (manifest handoff note) — the code as built
  assumes the tuple; if the answer is the phrase layer, `tracy_
  permissions.service.ts`'s output shape needs revisiting.
- Rendering `entry.user_permissions` into the model's system prompt —
  explicitly out of scope for this pass; the field is populated and
  stored, nothing reads it yet.
- Neither repo's changes are committed yet.
