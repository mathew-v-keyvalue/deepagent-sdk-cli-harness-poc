# morpheus_backend changes

Repo: `morpheus_backend`. Branch: `v2/wire-tracy`.

## Before

morpheus_backend had no route for Tracy at all — it never talked to
deepagent-sdk-cli-poc. Its other AI integrations (`super_search`,
`dashboard_builder`) proxy to a different service (`AI_AGENTS_URL`) using
Basic-Auth service-to-service trust plus the user's JWT forwarded as
headers.

## After: a new, fully additive `tracy` module

New directory: `morpheus/src/modules/tracy/`, structurally mirroring
`dashboard_builder`'s existing SSE-proxy pattern but deliberately
**stateless** (no DB writes — see [known-limitations.md](known-limitations.md))
and **not** using `AI_AGENTS_URL`/`BASIC_AUTH` at all — this is a separate
service with its own separate config, so nothing about the existing
`super_search`/`dashboard_builder` integrations was touched.

### `dtos/tracy.dto.ts`

```typescript
export const TracyChatBodyDto = Type.Object({
  message: Type.String(),
  sessionId: Type.String(), // required — the frontend always sends one, see session-handling.md
});
```

### `services/tracy_chat.service.ts`

`streamTracyChat(body, authData, requestId, traceHeaders?)`:

- Reads `DEEPAGENT_URL` and `DEEPAGENT_SERVICE_AUTH` from env; throws if
  either is unset (fail-fast, same pattern `dashboard_chat.service.ts`
  already uses for its own `AI_AGENTS_URL`/`BASIC_AUTH`).
- POSTs `application/x-www-form-urlencoded` (not multipart — deepagent's
  `/chat` is a FastAPI `Form(...)` handler, which accepts either, and
  `URLSearchParams` is simpler than building multipart in Node).
- Body fields: `message`, `session_id: body.sessionId`,
  `access_token: authData.token` — **the raw FusionAuth JWT**, forwarded
  unchanged. This one line is the entire mechanism that makes the AI
  service's sandboxed CLI calls run as the real requesting user — see
  [auth-flow.md](auth-flow.md).
- Headers: `X-Service-Auth: ${DEEPAGENT_SERVICE_AUTH}` (proves this call
  comes from morpheus_backend), `x-request-id`, `x-session-id` (log
  correlation), and any inbound `traceparent`/`tracestate` headers
  forwarded through unmodified (not consumed by anything today, but kept
  for future eval/tracing work rather than silently dropped).
- SSE frame-buffering: the same `\n\n`-boundary buffering approach as
  `dashboard_chat.service.ts`, duplicated locally (~20 lines) rather than
  imported across module boundaries — keeps this module self-contained and
  additive, not coupled to `dashboard_builder` internals.

### `handlers/tracy_chat.ts`

`handleTracyChat` — same raw-stream CORS-header + `flushHeaders()` +
abort-on-client-disconnect pattern as `dashboard_chat.ts` (needed because
`@fastify/cors`'s normal header-setting doesn't reach a raw-stream
response). Unlike `dashboard_chat.ts`, there is **no persistence logic at
all** — every SSE frame from deepagent is forwarded essentially verbatim
(its own event vocabulary — `session`/`delta`/`tool_use`/`done`/`error` —
needs no transformation). One exception: `error` frames are sanitized
before reaching the browser — deepagent's `harness_error` messages can
carry raw upstream exception text, so the `code` is preserved (useful for
client-side branching) but the `message` is replaced with a generic one;
the full detail is still logged server-side.

### `routes/index.ts`

```typescript
export default class TracyRoutes {
  public prefix_route = '/tracy';
  public static readonly role = 'service';
  // POST /chat, preHandler: [verifyToken], no Casbin permission gate
}
```

No permission gate — matches `super_search`'s lighter pattern (a general
chat assistant available to any authenticated user), not
`dashboard_builder`'s heavier, permissioned pattern.

### Wiring

`morpheus/src/server.ts`: `TracyRoutes` imported and added to the app's
`routes` array (order doesn't matter — each module registers under its own
`prefix_route`). Registration is gated by `SERVICE_ROLES` at boot
(`app.ts`'s own `routes()` filters by `route.role`) — `TracyRoutes.role =
'service'` matches `SERVICE_ROLES`'s default inclusion of `service`, same
as `SuperSearchRoutes`/`DashboardBuilderRoutes`.

**Operational note (the actual cause of a real 404 hit during testing):**
this repo's dev process runs via `nodemon --watch 'dist/**/*'` — it
watches the *compiled* output, not the TypeScript source. A new module
under `src/modules/tracy/` is invisible to the running server until
`npm run build` (`tsc --strict false && tsc-alias`) actually compiles it
into `dist/`. If `/tracy/chat` ever 404s after a fresh checkout or a new
change to this module, build first, don't just restart.

## New env vars

| Var | Purpose |
|---|---|
| `DEEPAGENT_URL` | Base URL of deepagent-sdk-cli-poc. Local dev: `http://localhost:8010`. |
| `DEEPAGENT_SERVICE_AUTH` | Shared secret. Must exactly match the same-named var on the AI service side. |

Full reference across all three repos: [deployment-config.md](deployment-config.md).

## Files touched

| File | Change |
|---|---|
| `morpheus/src/modules/tracy/dtos/tracy.dto.ts` | New |
| `morpheus/src/modules/tracy/services/tracy_chat.service.ts` | New |
| `morpheus/src/modules/tracy/handlers/tracy_chat.ts` | New |
| `morpheus/src/modules/tracy/routes/index.ts` | New |
| `morpheus/src/server.ts` | Import + register `TracyRoutes` |
| `.env.example` | Document `DEEPAGENT_URL`, `DEEPAGENT_SERVICE_AUTH` |
