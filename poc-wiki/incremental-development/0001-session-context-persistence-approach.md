# 0001 — Session context persistence: write-behind + cold-start rehydrate

Status: **design discussion only, nothing implemented yet.**

## What was decided

Not swapping `harness/agent.py`'s `_checkpointer` (LangGraph `InMemorySaver`)
for a Postgres-backed saver outright. Instead: keep `InMemorySaver` as the
hot path for a session's life within one pod (fast, zero I/O per turn —
matches today's behavior), and add Postgres as a **write-behind + cold-start
rehydrate** layer on top:

- Periodically flush session context to Postgres once it grows past some
  threshold (message count / token estimate — not yet decided), so a long
  session doesn't grow the in-memory store unboundedly.
- Flush again on graceful pod shutdown, so short sessions that never crossed
  the size threshold aren't lost.
- On a fresh pod's first sight of a `thread_id`/`session_id` not present in
  its own in-memory checkpointer, check Postgres and rehydrate before
  continuing, instead of silently starting a blank conversation (today's
  behavior for any unseen `thread_id` — see `server/sessions.py`'s module
  docstring).

## Why

Today both existing stores (`server/sessions.py`'s `SessionStore` and
`harness/agent.py`'s `_checkpointer`) are in-process memory — effectively
free compared to any real I/O. A straight swap to a Postgres-backed
checkpointer would add a DB round-trip to every single turn. Write-behind
keeps the fast path fast and only pays for Postgres when actually needed
(size threshold or shutdown).

## Open items — needs DevOps input before building the shutdown flush

Agreed these three need confirming with DevOps before relying on a
`SIGTERM`-triggered flush in production:

1. **Termination grace period** — whatever orchestrates these pods sends
   `SIGTERM`, waits some grace window, then `SIGKILL`s. If the flush takes
   longer than that window under real load, it dies mid-write. Need to know
   the actual configured value (e.g. Kubernetes'
   `terminationGracePeriodSeconds`) and whether it's enough.
2. **Whether `SIGTERM` even reaches the process** — depends on the
   container's entrypoint shape (exec-form `CMD` vs a shell-wrapped one,
   any process supervisor in front). DevOps/whoever owns the Dockerfile
   needs to confirm this.
3. **Postgres reachability at shutdown** — confirm network egress/creds to
   Postgres aren't torn down before the app's shutdown hook gets to run.

Implementation note (agreed, not a DevOps question): hook into FastAPI/
uvicorn's lifespan/shutdown event rather than a raw `signal.signal()` call —
uvicorn already intercepts `SIGTERM` for its own graceful shutdown; a raw
handler risks fighting that instead of composing with it.

## LangGraph capability check (confirmed against the installed package)

Checked `.venv` directly (`langgraph==1.2.11`, `langgraph-checkpoint==4.2.0`,
only `base` and `memory` submodules installed — no `postgres`/`sqlite`/
`redis` checkpoint savers present) before assuming anything from docs:

- No built-in hybrid/tiered checkpointer ships in the base package —
  `BaseCheckpointSaver` is a single-backend interface (`get`/`put`/`list`/
  `delete_thread`/`copy_thread`/`prune`). The write-behind + rehydrate
  pattern above is not a config flip; it's custom harness code, most likely
  built on the checkpointer's own `get_tuple()`/`put()` primitives.
- `delete_thread(thread_id)` and `prune(thread_ids, strategy="keep_latest"|
  "delete")` are standard on any saver implementation — real, reusable
  building blocks for trimming/eviction if that work ends up touching the
  checkpointer itself, not just `SessionStore`'s metadata.
- `copy_thread(source, target)` exists but only copies within one saver's
  own storage — not a memory→Postgres export mechanism, doesn't give the
  flush step for free.
- `InMemorySaver` has an internal `factory=` hook that can back its storage
  with `PersistentDict` (local pickle file, write delayed until close/sync)
  — a write-behind pattern already exists in the package, but it's
  local-disk-only, undocumented as public API, and not usable for a pod (no
  durable local disk across restarts) or for Postgres. Noted as precedent,
  not reused.
- `InMemorySaver`'s own docstring: *"Only use InMemorySaver for debugging or
  testing purposes. For production use cases we recommend installing
  langgraph-checkpoint-postgres and using PostgresSaver /
  AsyncPostgresSaver."* — confirms Postgres-backed checkpointing is the
  sanctioned production path, but that's the full-swap pattern, not
  write-behind-over-memory. `langgraph-checkpoint-postgres` is not currently
  installed in this repo.

## Impact

No code changed yet. This settles the *shape* of the approach (write-behind
+ rehydrate, not a backend swap) and flags three DevOps confirmations and
one implementation preference (lifespan hook over raw signal handler) to
carry into whenever this actually gets built. Flush trigger (size threshold
vs. token estimate) and what gets flushed (raw messages vs. trimmed/
summarized) are still undecided — see the execution-modes wiki conversation
this grew out of.
