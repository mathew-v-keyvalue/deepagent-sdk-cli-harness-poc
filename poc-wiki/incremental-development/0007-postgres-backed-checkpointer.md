# 0007 — Postgres-backed checkpointer

Status: **implemented**, verified live. Not yet committed.

## What changed

- `pyproject.toml`: added `langgraph-checkpoint-postgres>=3.1.2` and
  `psycopg[binary,pool]>=3.2.0`.
- `.env.example` / `.env`: new `DATABASE_URL`, pointed at morpheus_backend's
  own local Postgres instance (`localhost:5438`, db `morpheus`) — not a
  dedicated instance, matching the same "reuse what's there" direction
  already taken for the `ai_conversations`/`ai_messages` work on the
  backend side.
- `harness/agent.py`: `_checkpointer` is now a swappable module-level
  variable (`BaseCheckpointSaver`, default `InMemorySaver()`) with a
  `set_checkpointer(cp)` setter, instead of a fixed `InMemorySaver()`
  constant. `_build_agent`'s `checkpointer` param type widened from
  `InMemorySaver | None` to `BaseCheckpointSaver | None` to match.
- `server/app.py`: new `lifespan` context manager — opens a real
  `psycopg_pool.AsyncConnectionPool` (min_size=1, max_size=10) once for the
  process's lifetime when `DATABASE_URL` is set, wraps it in an
  `AsyncPostgresSaver`, calls the saver's (idempotent) `.setup()`, and
  swaps it in via `set_checkpointer()`. A no-op — logs a warning, otherwise
  unchanged behavior — when `DATABASE_URL` isn't set, so this harness still
  runs exactly as it always has for anyone not configuring it.
- `verify/verify_checkpoint_backup_restore.py`: new verify script.

### A second real gap, found and fixed after the initial live-verification pass

`AsyncPostgresSaver.from_conn_string()` — the obvious, documented way to
construct one — opens exactly **one raw connection**, not a pool,
confirmed by reading its source (`AsyncConnection.connect(...)`, no pool
involved), not assumed from `psycopg[pool]` being installed. Left as the
initial implementation, every concurrent request across every session
would have serialized through that single connection, and a dropped
connection would have broken the checkpointer for the rest of the
process's life with no automatic recovery.

Fixed by constructing an `AsyncConnectionPool` directly and passing it to
`AsyncPostgresSaver(conn=pool)` instead — confirmed via `_ainternal.Conn`'s
own type alias (`AsyncConnection | AsyncConnectionPool`) that this is a
genuinely supported construction path, not a hack. The pool's `kwargs`
replicate the exact connection settings `from_conn_string` itself requires
(`autocommit=True, prepare_threshold=0, row_factory=dict_row`) so pooled
connections behave identically — confirmed live with a real `.setup()` +
`aget_tuple()` round trip before wiring it into `server/app.py`.

## Why

`deepagent-sdk-cli-poc` is going to production, not staying a POC — see
[[project-deepagent-bounded-chat-context]] (session memory) for the fuller
context. An in-memory-only checkpointer isn't just non-durable, it's
incorrect the moment there's more than one app instance: a request for an
existing session can land on a process that's never seen it. The fix is to
make the checkpointer itself durable and shared.

## The design that was tried first, and found broken via live testing

The original plan (agreed on before implementation, in the same session)
was more conservative about cost: keep `InMemorySaver` as the *live*,
per-step checkpointer (unchanged, zero added Postgres latency during a
turn), and layer a separate `AsyncPostgresSaver` "backup" on top — touched
only twice per turn, save at the end (overwrite, not accumulate — a "save
slot" not a version history) and restore-if-cold at the start.

**Live testing found this silently corrupts message history.** After
saving turn 1, wiping the in-memory checkpointer's state for that thread
(simulating a fresh pod), and restoring from the Postgres backup before
turn 2: the restored state had **zero messages** — no error, no exception,
just an empty conversation. Traced to the root cause: LangGraph's
`messages` channel is delta-based internally (confirmed directly —
`InMemorySaver.get_tuple`'s own source calls `self._load_blobs(...,
checkpoint_["channel_versions"])` to reconstruct a channel's full value;
copying only the *latest* checkpoint's `channel_values` between two
independent checkpointer instances drops the ancestor chain that
reconstruction depends on). `BaseCheckpointSaver.acopy_thread`'s own
docstring confirms this is a known, named failure mode ("the copy must
carry the complete parent chain ... or `DeltaChannel`s will silently
reconstruct as empty") — this wasn't an edge case, it's how the library's
own maintainers describe exactly this mistake.

Considered using `aprune(strategy="keep_latest")` instead of hand-rolled
copying, since its docstring describes exactly the intended behavior — but
it raises `NotImplementedError` in the installed package version
(`langgraph-checkpoint-postgres==3.1.2`), confirmed live, not assumed from
the interface declaration.

**Corrected design**: don't run two checkpointer instances at all. Swap
`_checkpointer` for the real `AsyncPostgresSaver` outright — one instance
handles every read and write, so there's no cross-backend copying, no
"cold" concept, and no delta-chain integrity to preserve by hand. Restoring
a session on a different process now works because LangGraph's own resume
logic already does it correctly against a single, consistent, shared
checkpointer — nothing custom needed.

## Disclosed tradeoff, not solved here

Storage now grows per graph-step (10-19 rows per turn, per this week's own
measurements), not per turn — `aprune` isn't usable yet to bound this (see
above). Treated the same way "clean up inactive sessions" was already
deferred earlier in the design discussion: a real, acknowledged gap, not
an oversight, revisit if/when it's an actual operational problem.

## Verified

- `python3 -m py_compile` on all touched files.
- `verify/verify_checkpoint_backup_restore.py`, run live against the real
  local Postgres instance: a turn run through `harness.agent.stream()`
  with the checkpointer swapped to `AsyncPostgresSaver`, followed by a
  second turn built from a *fresh* `_build_agent()` call (nothing reused
  from turn 1 except the swapped-in checkpointer itself, the one thing
  that would genuinely persist across a real process restart) — both
  turns' real content present afterward, confirmed both via `graph.aget_state()`
  and a direct `aget_tuple` read straight off Postgres.
- All four pre-existing verify scripts (`verify_server_recent_actions.py`,
  `verify_server_user_permissions.py`, `verify_server_nonuuid_session_id.py`,
  and the above) still pass individually against the unchanged default
  (`DATABASE_URL` unset in their own in-process ASGI test context) —
  confirms the swap is a true no-op for existing callers that don't
  configure it.
- Ran the actual `uvicorn server.app:app` process live (real startup,
  real `lifespan` hook, real `DATABASE_URL`) and sent real, non-scripted
  `/chat` requests through it — both before and after the connection-pool
  fix — confirming each real turn's checkpoint data actually lands in the
  real `checkpoints`/`checkpoint_blobs` tables (queried directly via
  `psql`-equivalent, not inferred from the harness's own logs).
- Table-collision check: queried `checkpoints` directly for all distinct
  `thread_id`s present — confirmed, at time of writing, only this
  harness's own test/verify/live-check sessions exist there. Not a
  guarantee nothing else could ever write to the same tables (LangGraph's
  `AsyncPostgresSaver.setup()` uses fixed, unprefixed table names — no
  namespacing built in), just confirmed no current collision.

## Impact

Session durability now works, for real, against the actual failure mode
this was meant to fix (a request landing on a different process than the
one that started the conversation) — not just "the code compiles." The
[[project-deepagent-bounded-chat-context]] memory's note about a
"restore-if-cold" mechanism is now stale — superseded by this simpler,
correct design; nothing like that exists or is needed.
