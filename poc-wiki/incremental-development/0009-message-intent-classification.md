# 0009 — Message intent classification

Status: **implemented**, verified live. Not yet committed.

## What changed

- `harness/agent.py`:
  - `MessageIntentLabel` — a `Literal` of 7 categories (`read_query`,
    `write_action`, `workflow_request`, `capability_question`,
    `clarification_or_followup`, `conversational`, `report_request`).
  - `_IntentClassification` (a `pydantic.BaseModel`) / `_classify_message_intent(prompt)`
    — one LLM call via `resolve_model().with_structured_output(...)`,
    constrained to exactly one of the 7 labels by the provider's own
    structured-output machinery. Returns `None` on any failure; never
    raises.
  - `stream()`: fires `_classify_message_intent` as a background
    `asyncio.create_task` as early as possible (right after `thread_id` is
    resolved, before the graph even runs) — concurrent with the whole
    turn, not sequential. Awaited (with a 5s timeout) right before the
    `turn_done` log line, which now also logs `intent=`. Explicitly
    `.cancel()`'d on the turn's failure path, so a task is never abandoned
    mid-flight.
  - `Done` gained `intent: MessageIntentLabel | None = None`.
- `server/sessions.py`: `MessageIntent` dataclass (`intent`,
  `message_preview`, `timestamp`) and `SessionEntry.recent_intents` — same
  bounded-ring-buffer shape and `RECENT_ACTIONS_MAXLEN` cap as
  `recent_actions`.
- `server/app.py`: `Done`'s `Done` handler appends to
  `entry.recent_intents` when `event.intent is not None`. Deliberately
  **not** added to the client-facing SSE `done` payload — same posture as
  `recent_actions`/`chat_summary`, neither of which are exposed there
  either.
- `verify/verify_message_intent_classification.py`: new verify script.
- `verify/verify_server_recent_actions.py`, `verify_server_user_permissions.py`,
  `verify_server_nonuuid_session_id.py`, `verify_checkpoint_backup_restore.py`,
  `verify_bounded_chat_context.py`, `verify_shell_sandbox_denies.py`: each
  script's scripted model updated (see "A real regression" below).

## Why

Last pending item from the original Session Store spec ("Message context:
Intent of a message"). Nothing consumes it yet — no routing/branching
logic exists on top of this, deliberately, per direction. Same "build the
field before its consumer exists" precedent as `recent_actions`
(0002), which also isn't exposed anywhere yet.

## The taxonomy, and why it's shaped this way

Explored first, not guessed: this system deliberately has **no hardcoded
CLI/resource vocabulary** — everything is discovered live via `cybersierra
manifest --raw` (`skills/cyber-sierra/_internal/planner/references/
planning-rules.md`: "No hardcoded CLI knowledge"). The real vocabulary of
what this system actually does lives in `eval/CLI_COMMAND_REFERENCE.md` —
modules: `health`, `permissions`, `notifications`, `user-management`, and
`tprm` (`assessments`, `assessees`, `activity-logs`, `dashboard`,
`risk-score-config`, `vendor-info`).

The 7 intent categories are the *shape* of a request, cutting across all
of those resources, not a second copy of the resource list — a
resource-tagged taxonomy would need constant upkeep against a manifest
that's intentionally never enumerated in this codebase, exactly the kind
of hardcoding this system already avoids on purpose.

## Guardrails, explicitly (this was the point of using structured output)

1. **Output shape**: `with_structured_output()` + a `Literal` type — the
   model cannot return anything outside the 7 categories, enforced by the
   provider's own structured-output machinery, not string-matched after
   the fact. Confirmed live: 4/4 real test prompts returned exactly one of
   the 7 valid labels, matching the expected category in every case.
2. **Never blocks the turn**: fire-and-forget `asyncio.create_task`, only
   awaited (with a timeout) once the graph has already finished running.
3. **Never fails the turn**: every failure path degrades to `intent=None`
   plus a logged warning — same advisory-degrade posture already used for
   permissions and chat summary.
4. **No orphaned tasks**: explicit `.cancel()` on the turn's failure path.

## Two real bugs found via live testing, not just code review

### 1. A genuine regression across every sibling verify script

The very first live run of an *existing* verify script
(`verify_server_user_permissions.py`) started failing every single turn
with a spurious `harness_error` — despite that script's own assertions
still reporting PASS. Root cause: every sibling verify script's scripted
model (`ScriptedToolCallModel`) is a fixed-length response queue
(`self.responses[self._i]`, incrementing per call), sized for exactly the
number of calls each script's *own* scenario intentionally makes.
`_classify_message_intent`'s incidental `resolve_model()` call — mocked to
that same instance, since every script mocks `resolve_model` globally —
silently consumed one extra slot from that queue, either running the
queue past its end (`IndexError`) or, worse, running *concurrently* with
the main turn and non-deterministically consuming a slot meant for a
*specific step* in a sequenced script (confirmed: a bare clamp/repeat-last
fix wasn't sufficient for this reason — a scripted response meant for
step 2 of a sequence could get consumed by classification instead,
corrupting the intended step-by-step content regardless of not crashing).

Fixed properly, not just clamped: every affected scripted model now
recognizes the classification prompt's distinctive text ("Classify the
shape of this user message") and short-circuits with a generic reply
**without incrementing its own index at all** — so the shared queue is
completely untouched by classification, regardless of asyncio scheduling
order. Applied to all 6 affected verify scripts (5 index-based
`ScriptedToolCallModel`s, plus `verify_bounded_chat_context.py`'s
content-routing `RouterModel`, which needed the same recognition branch
so classification calls didn't get miscounted as real main-turn calls).

### 2. `verify_message_intent_classification.py`'s own real-model tests initially failed for an unrelated environment reason

Part 1 (real model, no mocking) failed 4/4 with `intent=None` on first
run. Root cause, found by bypassing the function's own swallowing
try/except to see the real exception: `harness/agent.py` never calls
`load_dotenv()` itself — every sibling verify script gets `.env` loaded
"for free" as a side effect of importing `server.app`, which does. This
script only needs `harness.agent` directly, so without an explicit
`load_dotenv()` call, `AGENT_MODEL`/API keys silently fell back to
whatever was stale in the raw shell environment (a different, unconfigured
provider) rather than raising an obvious, immediate error. Fixed by
calling `load_dotenv()` explicitly at the top of the script. Not a bug in
`_classify_message_intent` itself — confirmed once fixed, real
classification worked correctly and consistently.

## Verified

- `python3 -m py_compile` on all touched files.
- `verify/verify_message_intent_classification.py`:
  - Part 1 (real model, 4 real prompts reused from this week's own live
    demos): all 4 returned a valid label, matching the expected category
    in every case (`read_query`, `conversational`, `capability_question`,
    `workflow_request`).
  - Part 2 (scripted main turn): confirms the turn completes successfully
    with `intent=None` when classification can't produce genuine
    structured output against a scripted model — the exact regression
    check for bug #1 above.
- All 6 previously-existing verify scripts re-run individually and
  confirmed still passing after the fix.
- Live end-to-end against the real running server (`uvicorn server.app:app`,
  real `DATABASE_URL`, real model): sent `"hi there"`, confirmed
  `turn_done ... intent=conversational` in the real log, correctly
  classified, real turn, real provider — not inferred from a script.

## Impact

`SessionEntry.recent_intents` now exists and is populated correctly on
every successful classification. Not exposed over the SSE contract or any
endpoint yet — deliberately, same posture as `recent_actions`. Any future
consumer/router would read this field; nothing currently does.
