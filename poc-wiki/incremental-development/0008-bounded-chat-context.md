# 0008 — Bounded chat context (last-N-turns + rolling summary)

Status: **implemented**, verified live. Not yet committed.

## What changed

- `harness/agent.py`:
  - `CHAT_CONTEXT_WINDOW_TURNS = 6` — the window size, a named constant,
    same "easily-changed starting point" posture as the existing
    `RECENT_ACTIONS_MAXLEN`.
  - `_turn_boundaries(messages)` — finds each `HumanMessage`'s index; a
    "turn" is one user-visible exchange (a `HumanMessage` plus everything
    the agent did in response), not one raw LangGraph message.
  - `_render_chat_summary(summary)` / `_update_chat_summary(previous,
    newly_expired_messages)` — the latter is one LLM call (via the same
    `resolve_model()` the main agent uses — no separate "cheap model"
    config yet) that folds in *only* the turns that just aged out, never
    the full history.
  - `_ChatSummaryResult` / `_BoundedContextMiddleware` — a
    `langchain.agents.middleware.AgentMiddleware` implementing
    `awrap_model_call`: trims `request.messages` to the last N turns and
    appends the rendered summary to the system message, via
    `request.override(...)` — never touches persisted checkpoint state,
    same principle as 0007's own checkpointer design.
  - `_build_agent`/`stream()` gained `chat_summary`/
    `chat_summary_covers_turns` params, threaded into the middleware and
    back out via new fields on `Done`.
- `server/sessions.py`: `SessionEntry.chat_summary: str = ""` and
  `chat_summary_covers_turns: int = 0`.
- `server/app.py`: both `stream(...)` call sites pass
  `entry.chat_summary`/`entry.chat_summary_covers_turns` in; the `Done`
  handler writes them back — but only when `event.chat_summary is not
  None`, since `None` means "unchanged this turn" (short session, or
  summarization failed), not "clear it."
- `verify/verify_bounded_chat_context.py`: new verify script.

## Why

Already-observed real cost: this week's own live demo turns hit
269K-552K total tokens on single conversations, because the checkpointer
(0007, and the `InMemorySaver` before it) replays the *entire* message
history to the model every turn, unbounded. Capping what's replayed keeps
per-call cost bounded on a long session without losing continuity — the
information isn't dropped, it's compressed into a summary.

Design was fully settled via a dedicated grilling/design session before
implementation (see session memory
[[project-deepagent-bounded-chat-context]] for the full record of that
decision process — mechanism choice, window unit, failure mode, etc.). Key
decisions, restated here since they're the reasons the code looks the way
it does:

- **Mechanism**: `AgentMiddleware.awrap_model_call`, not touching the
  checkpointer — confirmed via `ModelRequest`/`AgentMiddleware`'s actual
  interface (`before_model`'s return value would apply as a *persisted*
  state update via the reducer; `wrap_model_call`/`awrap_model_call`
  intercepts only what's sent to the model for that one call). Getting
  this wrong would have reintroduced exactly the kind of bug 0007 found —
  silently mutating what's supposed to be authoritative history.
- **Window unit is turns, not raw messages** — confirmed live this week
  that a single turn can be 13-19 raw messages (tool calls/results), so a
  raw-message window would be consumed by one turn.
- **Summary regeneration is incremental** — `chat_summary_covers_turns`
  exists specifically so a later turn knows exactly which turns are
  *newly* expired since the summary was last updated, rather than
  re-summarizing full history every time (which would silently reintroduce
  the unbounded-cost problem this exists to solve).
- **Failure mode**: keep the previous summary, log a warning, never fail
  the turn — same advisory-degrade posture as the malformed-permissions-JSON
  handling in `server/app.py`.
- **No-op below the window**: sessions at or under `CHAT_CONTEXT_WINDOW_TURNS`
  turns never trigger any of this — no summarization call, full history
  passes through untouched, matching `_render_user_permissions`'s own
  "nothing sent when there's nothing to send" shape.

## A test-script bug worth recording (not an implementation bug)

The first run of `verify_bounded_chat_context.py` failed at the
incremental-summarization check, appearing to show turn 1's content
re-appearing in a later summarization call. Root cause was in the
*verify script*, not `harness/agent.py`: `_run_turn` never threaded
`chat_summary`/`chat_summary_covers_turns` between calls, so every turn
silently started from `covers_turns=0` regardless of what a prior turn had
actually returned — exactly mirroring the one thing `server/app.py`'s real
`Done` handler does correctly and the test script initially didn't. Fixed
by threading state between `_run_turn` calls the same way `server/app.py`
does between requests. Recorded here because it's a good illustration of
why "verify live" only proves what the verification script actually
exercises — this would have looked identical to a real bug from the
script's output alone.

## Verified

- `python3 -m py_compile` on all touched files.
- `verify/verify_bounded_chat_context.py`, run live (scripted router
  model, real `harness.agent.stream()` calls, real middleware): 8 real
  turns run in sequence, inspecting what the model *actually received*
  each time (not what the harness merely reports):
  - Turns 1-6 (at/under the window): full history every time, `Done.chat_summary`
    stays `None`.
  - Turn 7 (first turn over the window): `chat_summary_covers_turns`
    becomes 1, turn 1's raw content is gone from the model call, the
    system message carries a rendered summary mentioning turn 1.
  - Turn 8: `chat_summary_covers_turns` advances to 2, turn 2's raw content
    is now also gone, and — checked directly against the actual
    summarization prompt text — turn 1 is *not* re-included in the
    "newly expired" section, only turn 2 is (proving genuine incremental
    behavior, not a full re-summarize each time).
- All four pre-existing verify scripts still pass individually against the
  unchanged default (`chat_summary=""`, `chat_summary_covers_turns=0`,
  both under the window in every case) — confirms this is a true no-op for
  existing callers.

## Impact

Long sessions now have a bounded per-call token cost. Nothing about the
SSE contract or checkpointer (0007) changed — this only shapes what's sent
to the model per call, exactly as designed. `SessionEntry.chat_summary` is
new session-context state alongside `recent_actions`/`user_permissions`;
not yet exposed over any endpoint, same "not yet surfaced, deliberately"
posture as `recent_actions` in 0002.
