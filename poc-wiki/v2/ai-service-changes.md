# deepagent-sdk-cli-poc changes (this repo)

Branch: `v2/wire-tracy`. This is where the most complex and most
security-sensitive part of v2 happened — the full reasoning for the auth
pieces is in [auth-flow.md](auth-flow.md); this document is the file-by-file
"what changed."

## `server/app.py`

- **New: `_verify_service_auth`** — a FastAPI dependency on `POST /chat`
  only (`GET /health` and the bundled `frontend/index.html` stay open).
  Compares an `X-Service-Auth` header against `DEEPAGENT_SERVICE_AUTH`
  (`hmac.compare_digest`, fails closed if unset). Closes what was
  previously a real hole: `/chat` had **zero** service-level auth — CORS
  only stops browser JS, not a direct or server-to-server call, meaning
  anyone who could reach the port could execute real CLI commands.
- **New: a shared `_http_exception_handler`** so this new 401 (and any
  future `HTTPException`) comes back in this server's one existing error
  shape (`{"error": {"code": ..., "message": ...}}`), not FastAPI's
  default `{"detail": ...}`.
- **Changed session-ID handling**: a caller-supplied `session_id` unknown
  to the store is now treated as the first turn of a client-chosen
  session instead of `404 unknown_session`. Full reasoning and the
  tradeoff this accepts: [session-handling.md](session-handling.md).
- **Changed `tool_use` SSE event**: now includes `args`, not just `name`
  (`{"name": event.name, "args": event.args or {}}`). Needed for a
  planned later piece of work (scoring which CLI commands the agent
  actually invoked) — not used by anything in this v2 wiring itself, but
  added here since it touches the same event.
- Updated module docstring and the CORS comment block — both previously
  described morpheus_fe calling this server directly, now stale since
  Tracy goes through morpheus_backend instead. CORS/`FRONTEND_ORIGINS`
  itself is left in place, harmless, still used by the bundled
  `frontend/index.html` test page.

## `server/sessions.py`

`SessionStore.create()` now accepts an optional caller-supplied ID instead
of always minting its own. See [session-handling.md](session-handling.md)
for the full reasoning, including a corrected module docstring (a
prior in-session correction pass initially mis-stated the resulting
behavior — the docstring now accurately says `404 unknown_session` is no
longer reachable through any code path here).

## `harness/agent.py`

- **`MORPHEUS_BASE_URL` injected unconditionally** — the fix for bug #2 in
  [auth-flow.md](auth-flow.md). Deployment-wide, independent of the
  per-user token toggle.
- **`MORPHEUS_TOKEN`, not `CYBERSIERRA_TOKEN`** — the fix for bug #1 in
  [auth-flow.md](auth-flow.md). Same injection point, corrected variable
  name, with an explanatory comment documenting the wrong-name history so
  it can't quietly regress.
- **New system-prompt guidance**: if a command's output indicates an
  invalid/expired/unauthorized credential, tell the user in plain language
  to sign in again on the platform — never mention any command, env var,
  or internal auth mechanism. Added to `SYSTEM_PROMPT_APPENDIX`.
- **New, optional warning log**: `cybersierra_base_url_missing`, fired if
  `CYBERSIERRA_INJECT_ACCESS_TOKEN` is set but `CYBERSIERRA_BASE_URL`
  isn't — flags the exact misconfiguration bug #2 was, cheaply, in
  `logs/harness.log`.

## `harness/sandbox.py`

- **`DENIED_COMMAND_PREFIXES` widened** from two enumerated subcommands
  (`login-browser`, `login`) to the entire `cybersierra auth` group,
  with a new **`ALLOWED_DESPITE_DENIED_PREFIXES`** carve-out for
  `cybersierra auth whoami`. `is_command_allowed` checks the carve-out
  before the group deny. Full reasoning: [auth-flow.md](auth-flow.md).
- `_denial_message`'s auth-specific text updated (no longer assumes a
  human interactively logged in before the server started — not true for
  a real deployment after this work).

## `verify/` — extended and one new script

- **`verify/_server_helper.py`**: new shared `TEST_SERVICE_AUTH` constant;
  `running_server()` now always sets `DEEPAGENT_SERVICE_AUTH` in the
  subprocess it launches (every verify script that hits `/chat` would
  otherwise 401 against the new service-auth gate).
- **`verify/verify_server_session_context.py`**,
  **`verify/verify_server_streaming_incremental.py`**,
  **`verify/verify_server_multi_session_isolation.py`**: each now sends
  `X-Service-Auth: TEST_SERVICE_AUTH` via their `httpx.AsyncClient`'s
  default headers — same reason.
- **`verify/verify_shell_sandbox_denies.py`**: new `check_auth_group_denials()`
  — confirms `poll`/`set-token`/`logout` are all denied
  (`exit_code == DENY_EXIT_CODE`) and `whoami` remains explicitly allowed.
- **New: `verify/verify_server_fresh_deployment_no_profile.py`** — the
  regression test for bug #2: launches the server with a genuinely empty
  `HOME`, positive check (base URL set → real backend reached) and
  negative control (base URL unset → original failure reproduces, proving
  the positive check is meaningful). Full detail in
  [auth-flow.md](auth-flow.md).

## `dataset/run_dataset.py`

New `--service-auth` flag (default `$DEEPAGENT_SERVICE_AUTH`), required —
this pre-existing dev tool hits a running server's `/chat` directly and
would otherwise be silently broken by the new service-auth gate.

## `.env` / `.env.example`

New/changed vars, all documented in [deployment-config.md](deployment-config.md):
`DEEPAGENT_SERVICE_AUTH` (new), `CYBERSIERRA_INJECT_ACCESS_TOKEN` (comment
corrected — was documented backwards, see [auth-flow.md](auth-flow.md)),
`CYBERSIERRA_BASE_URL` (comment corrected — was previously dead config,
now actually wired up).

## `README.md` / `ARCHITECTURE.md`

Both had stale/incorrect claims from before this work (the wrong env var
name, presented as "verified empirically") corrected in place, with
explicit correction notes explaining what was wrong and how it was caught
— not silently rewritten, since erasing the history would let the same
mistake resurface unnoticed. A short pointer section at the top of
`README.md` now redirects here (`poc-wiki/v2/`) as the authoritative v2
documentation, rather than duplicating it inline.

## Files touched

| File | Change |
|---|---|
| `server/app.py` | Service auth, session-ID handling, `tool_use.args`, stale comments |
| `server/sessions.py` | Caller-supplied session ID support |
| `harness/agent.py` | `MORPHEUS_TOKEN`/`MORPHEUS_BASE_URL` fixes, system-prompt guidance |
| `harness/sandbox.py` | Auth-group deny-by-default |
| `verify/_server_helper.py` | Shared test service-auth secret |
| `verify/verify_server_session_context.py` | Send service-auth header |
| `verify/verify_server_streaming_incremental.py` | Send service-auth header |
| `verify/verify_server_multi_session_isolation.py` | Send service-auth header, `MORPHEUS_TOKEN` naming fix |
| `verify/verify_shell_sandbox_denies.py` | New auth-group-deny checks |
| `verify/verify_server_fresh_deployment_no_profile.py` | New file |
| `dataset/run_dataset.py` | `--service-auth` flag |
| `.env`, `.env.example` | New/corrected vars |
| `README.md`, `ARCHITECTURE.md` | Corrections + pointer to this wiki |
