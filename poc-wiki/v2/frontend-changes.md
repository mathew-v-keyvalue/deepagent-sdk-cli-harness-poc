# morpheus_fe changes

Repo: `morpheus_fe`. Branch: `v2/wire-tracy`.

## Before

The Tracy chat widget (`src/components/AiChatWidget/`) called this AI
service **directly** from the browser: `fetchEventSource` to
`API_DEEPAGENT_CHAT_URL` (built from `REACT_APP_API_DEEPAGENT_URL`, a
different origin/host than the actual backend), multipart `FormData` body,
`access_token` hardcoded to `''` (never populated — effectively
unauthenticated), session ID waited on a server-originated `session` SSE
event.

## After

Tracy calls morpheus_backend's new `/tracy/chat` route instead — same
origin as every other API call the app already makes, going through the
real, existing auth path.

### `src/utils/api-routes.ts`

```diff
- // AI Chat widget (deepagent-sdk-cli-poc harness)
- export const API_DEEPAGENT_CHAT_URL = `${API_DEEPAGENT_URL}/chat`;
+ // AI Chat widget ("Tracy") — proxied through the Morpheus backend, no
+ // longer called directly (see the backend's src/modules/tracy).
+ export const API_TRACY_CHAT_URL = `${API_URL}/tracy/chat`;
```

### `src/components/AiChatWidget/utils/aiChatStream.ts`

Rewritten to mirror the existing `dashboard-builder` chat client's
pattern (`src/pages/dashboard-builder/utils/chatStream.ts`), the
established precedent for a backend-proxied AI feature in this codebase:

- **JSON body** (`{ message, sessionId }`), not multipart `FormData` —
  deepagent-sdk-cli-poc's own `/chat` is a plain FastAPI `Form(...)`
  handler, but the *backend's* new route takes JSON and translates it, so
  this client no longer needs to know the AI service's own request shape
  at all.
- **`Authorization: Bearer ${getToken()}` set manually** on the
  `fetchEventSource` call. Important, non-obvious detail:
  `@microsoft/fetch-event-source` is a standalone `fetch` wrapper that
  **bypasses** `src/utils/request.ts`'s umi-request instance entirely — so
  its `Authorization` interceptor does not apply here. This is why the
  header has to be set by hand, exactly as the dashboard-builder chat
  client already does; there's no way to "reuse" that interceptor for an
  SSE call.
- Dropped the hardcoded `access_token: ''` — the real, correct token now
  flows through the `Authorization` header to morpheus_backend, which
  handles forwarding it onward (see [backend-changes.md](backend-changes.md)).
- SSE event handling (`delta`/`tool_use`/`done`/`error`) needed no
  changes — the backend forwards deepagent's own event vocabulary
  verbatim.
- The `onSession` handler is now a no-op — the server no longer originates
  the session ID (see below), so there's nothing meaningful for it to do.

### `src/components/AiChatWidget/hooks/useAiChatStream.ts`

```diff
- const sessionIdRef = useRef<string | undefined>(undefined);
  ...
+ if (!sessionIdRef.current) sessionIdRef.current = crypto.randomUUID();

  abortRef.current = streamAiChat(
    { message: trimmed, sessionId: sessionIdRef.current },
-   { onSession: (sessionId) => { sessionIdRef.current = sessionId; }, ... }
+   { ... }
  );
```

Session ID is now minted **client-side, lazily, on first `send()`** — not
eagerly on mount, since this widget's `Drawer` stays mounted globally
(`BaseLayout.tsx`) whether or not the user ever opens it; minting on every
page load for every logged-in user regardless of whether they ever open
Tracy would be wasteful. See [session-handling.md](session-handling.md) for
the full reasoning and the corresponding backend-side change this
required.

### Cleanup: removed now-fully-dead config

Once Tracy no longer calls the AI service directly, `REACT_APP_API_DEEPAGENT_URL`
(and the `API_DEEPAGENT_URL` constant built from it) had zero remaining
consumers. Removed from:

- `.env` and `.env.example` (the env var itself)
- `config/config.ts` (both the destructured read and the `define` export)
- `src/typings.d.ts` (the ambient `declare const API_DEEPAGENT_URL: string`)

Verified zero remaining references via `grep -rn "API_DEEPAGENT_URL" src/
config/ .env .env.example` before removing, and re-ran the project's full
`tsc --noEmit` after — same 1387 pre-existing, unrelated type errors as
before the change (a large legacy codebase without a clean `tsc` gate),
zero new ones introduced.

## Files touched

| File | Change |
|---|---|
| `src/utils/api-routes.ts` | `API_DEEPAGENT_CHAT_URL` → `API_TRACY_CHAT_URL`, now points at the backend |
| `src/components/AiChatWidget/utils/aiChatStream.ts` | JSON body, manual auth header, dropped `onSession` |
| `src/components/AiChatWidget/hooks/useAiChatStream.ts` | Client-side lazy session ID minting |
| `config/config.ts`, `src/typings.d.ts`, `.env`, `.env.example` | Removed dead `API_DEEPAGENT_URL` config |
