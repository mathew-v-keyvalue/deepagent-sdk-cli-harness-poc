# Known limitations — deliberate scope calls, not oversights

Everything here was an explicit decision made during this work, not
something missed. Listed so nobody re-discovers these and wonders if
they're bugs.

## No conversation persistence

morpheus_backend's `tracy` module is a stateless proxy — no writes to
`ai_conversations`/`ai_messages` (the tables `super_search` uses). A page
reload starts a fresh session. This was decided explicitly: persistence is
a real, separate feature addition (schema reuse, history UI, retention
policy), not part of "wire it correctly." Worth a dedicated follow-up if
wanted.

## No full distributed tracing

Only the frontend-minted `session_id` is propagated across the three
services (as a body field / informational header), for log-level
correlation. Full OpenTelemetry trace-context propagation across all three
services was explicitly out of scope for this version — installing
Netra/OTel in morpheus_backend and morpheus_fe is a materially bigger lift
than this wiring work called for.

## Error-message handling for invalid/expired tokens is prompt-only

The system prompt instructs the model what to say when a credential is
invalid/expired (see [auth-flow.md](auth-flow.md)) — this is not a
deterministic code-level intercept of the CLI's raw error output. A model
could, in principle, phrase this inconsistently across turns, or fail to
follow the instruction in an edge case. A more robust version would detect
the CLI's specific auth-error JSON shape in `AllowlistedShellBackend.execute`
and rewrite the tool output before the model ever sees it (the same
mechanism `_denial_message` already uses for denied commands). Not built
now because the core requirement — the model never *acting* on an auth
error itself — is already fully guaranteed by the sandbox's auth-group
deny, independent of how well-phrased its explanation is.

## Org/tenant-not-needed assumption not re-verified with a live token post-fix

Confirmed by directly inspecting the CLI binary's HTTP client (no
`x-org`/`x-tenant` header exists anywhere in it — only `Authorization:
Bearer <token>`), and consistent with every test run this session. Not
re-confirmed with a fresh *real* (non-placeholder) token against a
tenant-scoped command specifically after the `MORPHEUS_BASE_URL` fix,
since the local persisted CLI profile used for earlier testing was
emptied mid-session. Low risk (the mechanism is identical either way,
and no login flow was ever exercised without success in every other test
this session), but flagged rather than silently assumed.

## One pre-existing verify script has an unrelated fragility

`verify/verify_server_multi_session_isolation.py`'s env-token-isolation
check asks the model to echo back an environment variable's value
(`MORPHEUS_TOKEN`) as a way to prove per-request isolation without needing
real credentials. The model now refuses this request, citing security
concerns about exposing credential-looking strings — a plausible model
safety behavior, not something introduced by this work (this exact check
was flagged "not run to completion" in this repo's own docs even before
any of this session's changes). The property it was trying to prove
(per-request env isolation) was independently confirmed live, multiple
times, via direct CLI/curl tests with distinct tokens during this session
— see [auth-flow.md](auth-flow.md). The test script itself was not fixed;
it's outside this work's scope, flagged for whoever picks it up.

## What comes next (separate branch, not this one)

Evals — a scored dataset of queries, precision/recall on command
selection, wired to Netra so results are visible per-iteration. Fully
designed (see the plan history for this work) but deliberately not
started here, per explicit instruction to keep this branch scoped to the
wiring alone.
