# Session handling

## Who owns the session ID

**The frontend mints it**, lazily, on the first message send —
`crypto.randomUUID()` in morpheus_fe's `useAiChatStream` hook — not the
server. This matches the precedent already set by the product's
`dashboard_builder` chat feature (client mints once per page/session load)
rather than the old Tracy behavior (server minted it, returned via a
`session` SSE event).

Why the frontend, not the server:

- The ID exists before the very first network call, so it can be used to
  correlate logs/traces from the start, not after the fact.
- It matches existing precedent elsewhere in the product.
- It's simpler for the frontend to own its own conversation lifecycle than
  to wait on the server to hand one back.

## How it flows through the three services

```
morpheus_fe:  mint sessionId once, on first send
  → morpheus_backend: forwarded as `sessionId` in the JSON body, and also
    as an `x-session-id` header (informational, for log correlation)
    → deepagent-sdk-cli-poc: received as `session_id` form field on /chat
```

Every subsequent message in the same conversation reuses the same ID —
morpheus_fe never re-mints one mid-conversation, and this service treats a
repeated ID as "resume this conversation," not "start a new one" (see
below).

## The one code change this required in this repo

This surfaced a real compatibility gap: this service's `/chat` originally
only knew two cases —

- `session_id` **absent** → mint one server-side, this is a new session.
- `session_id` **present** → it must already be known to this server's
  session store, or `404 unknown_session`.

There was no way to accept a **caller-chosen, brand-new** session ID —
which is exactly what a frontend-minted ID is on its very first use. Fixed
in two files:

- **`server/sessions.py`**: `SessionStore.create()` now accepts an
  optional caller-supplied ID:
  ```python
  def create(self, session_id: str | None = None) -> str:
      session_id = session_id or str(uuid4())
      self._sessions[session_id] = SessionEntry()
      return session_id
  ```
- **`server/app.py`**: when `session_id` is present but unknown to the
  store, it's now treated as "first turn of a client-chosen session"
  (`store.create(session_id)`, still emits the `session` SSE event, still
  calls `stream(..., session_id=session_id)` not `resume=`) instead of
  404ing.

### The tradeoff this accepts, disclosed

This collapses two previously-distinguishable cases into one: "genuinely
new, frontend-minted ID, never sent before" (the case this exists for) and
"a real prior session whose ID this in-memory store has since forgotten —
e.g. a server restart wiped it" (previously a loud `404 unknown_session`)
are now indistinguishable from this store's point of view. The latter now
silently starts a brand-new, empty conversation instead of erroring —
mirroring exactly the LangGraph checkpointer's own long-documented
"unseen thread_id just starts fresh, no error" behavior. `404
unknown_session` is no longer reachable through any code path in this
service. This is an acceptable tradeoff for v2 (no session persistence
across restarts is already an accepted, documented limitation of this
service — see this repo's main README "Known limitations"), not a new
regression introduced silently.

## Not done in v2 (by explicit decision, not oversight)

- No persistence of conversation history in morpheus_backend — this
  remains a stateless proxy. A page reload starts a fresh session; this is
  expected behavior for this version, not a bug.
- No distributed OTel trace propagation using this session ID across all
  three services — only the ID itself is forwarded, for log-level
  correlation. See [known-limitations.md](known-limitations.md).
