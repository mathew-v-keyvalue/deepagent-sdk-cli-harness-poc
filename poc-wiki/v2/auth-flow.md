# Auth flow — the complete story

This is the most important document in this folder. Read it before
changing anything auth-related in any of the three repos.

## The one requirement everything below serves

> The end user's only login, ever, is logging into the CyberSierra
> platform once. Everything downstream must work silently and
> automatically — no browser popup, no interactive login prompt, no
> dependency on a human having manually set anything up on the server
> host, ever, on any deployment, including one that's never been touched
> by a human at all.

Every decision in this document exists to satisfy that one sentence.

## Two auth layers, at two different boundaries, never conflated

| Layer | Checked where | Proves | Fails how |
|---|---|---|---|
| `X-Service-Auth` header | `server/app.py`'s `_verify_service_auth` (this repo) | The *caller* is morpheus_backend at all | 401, fails closed if the shared secret is unset or wrong |
| `access_token` field | Not checked here — forwarded to the real CLI, which forwards it to the real backend | *Which real user* this call should act as | Whatever the real backend says (e.g. `"Invalid token"`) |

A caller can present valid service auth and still send no/garbage
`access_token` (falls back to whatever's in a persisted CLI profile, if
any — see below). It **cannot** skip service auth by presenting a valid
`access_token`. These two checks are independent on purpose — one is
about trusting the network path, the other is about identity.

### Why `X-Service-Auth`, not HTTP Basic-Auth

morpheus_backend's other AI integration (`AI_AGENTS_URL`) uses
`Authorization: Basic <base64>`. This integration deliberately does not
copy that shape: there's no real username/password pair here, just one
shared secret both sides already know, so a plain custom header compared
with `hmac.compare_digest` is the whole mechanism — no base64 encode/decode
ceremony that would only be meaningful if there were two distinct
credential components.

## How a user's own token becomes the CLI's real identity

1. morpheus_fe's request carries `Authorization: Bearer <user's JWT>`.
   morpheus_backend's `tracy` module (see [backend-changes.md](backend-changes.md))
   forwards that exact JWT as the `access_token` field on its call to this
   service.
2. This service's `/chat` receives it. `CYBERSIERRA_INJECT_ACCESS_TOKEN=1`
   (an existing, pre-built mechanism, off by default) is the switch that
   makes it matter at all.
3. `harness/agent.py`'s `_build_agent(access_token)` runs **fresh on every
   single turn** — not once at server startup, not shared across requests.
   It builds a brand-new Python dict as the subprocess environment. If
   `access_token` is non-empty and the switch is on:
   ```python
   env["MORPHEUS_TOKEN"] = access_token
   ```
   That's the entire mechanism. No parsing, no validation, no decoding —
   just a dict key.
4. That dict is handed to a **new** `AllowlistedShellBackend(env=env,
   inherit_env=False, ...)`. `inherit_env=False` means every real shell
   command this turn runs gets *only* this dict as its process
   environment — nothing from the server's own ambient environment leaks
   in, and nothing here leaks out to any other concurrent request (a new
   dict + new backend object every turn, never a shared/mutated global).
5. The **real, unmodified** `cybersierra` CLI binary resolves its own
   identity as:
   ```
   { baseUrl: process.env.MORPHEUS_BASE_URL ?? persistedProfile.baseUrl,
     token:   process.env.MORPHEUS_TOKEN   ?? persistedProfile.token,
     ... }
   ```
   (confirmed by directly reading the installed binary's own code — this
   is real product behavior, not something this integration added).
   Whatever token resolves gets attached as `Authorization: Bearer <token>`
   on every real backend call the CLI makes. The CLI does **zero** local
   validation of its own — the real backend is the sole authority on
   whether a token is valid. This is mechanically identical to what the
   browser frontend does (`src/utils/request.ts`'s own `Authorization:
   Bearer` interceptor) — from the backend's point of view, a request is a
   request, regardless of which client sent it.
6. When `access_token` is empty (or the switch is off), `MORPHEUS_TOKEN` is
   simply never added to the dict — not set to `""`. This matters because
   the CLI's `??` fallback only skips to the persisted profile on a
   *missing* key, not an empty string; an empty string would be treated as
   "here's your token, it's blank" and rejected, which is worse than no
   override at all.
7. The token is also passed as `redact=access_token` into the shell
   backend, scrubbing it from `cli_call_start`/`cli_call_done` **log
   lines** only — never from the actual response text sent back to the
   caller (a blanket scrub would defeat the isolation-proof verify script,
   which deliberately asks the model to echo an injected marker back).

## Bug #1 (found and fixed): the override variable name was wrong

Every version of this repo before this integration injected
`CYBERSIERRA_TOKEN`, believing that was the CLI's override variable. **It
was never correct.** Grepping the actual installed CLI binary
(`~/.cybersierra/bin/cybersierra`) for the literal string `CYBERSIERRA_TOKEN`
returns zero matches, anywhere. The real variable, found by grepping for
the profile-resolution object instead, is `MORPHEUS_TOKEN`.

Reproduced live, directly against the installed binary (real `HOME`, real
persisted profile from a prior login):

```
$ env -i CYBERSIERRA_TOKEN="fake.jwt.token" PATH="$PATH" HOME="$HOME" cybersierra auth whoami
{"data": {"profile": "default", "email": "...", ...}}   # ignored -- identical to no env var at all
$ env -i MORPHEUS_TOKEN="fake.jwt.token" PATH="$PATH" HOME="$HOME" cybersierra auth whoami
{"error": {"code": 2, "message": "Invalid token"}}      # read and used -- rejected because it's fake, not ignored
```

That second result — a real rejection, not a silent fallback to the
persisted profile — is what proves the override is actually read; a real
(non-fake) `MORPHEUS_TOKEN` would succeed as that identity instead. This
means `CYBERSIERRA_INJECT_ACCESS_TOKEN=1` was a **silent no-op** in every
prior version of this harness — every request ran as whatever identity was
persisted on disk, regardless of who was actually chatting. Fixed in
`harness/agent.py`, `harness/sandbox.py` (a comment reference), and
`verify/verify_server_multi_session_isolation.py` (which specifically
tests this exact mechanism).

The same investigation found `$CYBERSIERRA_PASSWORD` (mentioned in this
repo's own docs) is similarly wrong — the real variable is
`$MORPHEUS_PASSWORD`. Fixed in documentation.

## Bug #2 (found and fixed): nothing forwarded a base URL, either

Even with bug #1 fixed, testing against a **genuinely fresh** deployment
host (empty `HOME`, no `~/.cybersierra/config.json` at all — exactly what
a real, freshly-provisioned server looks like, since no human ever
manually logs in on a real multi-tenant production box) still failed every
command:

```
{"error": {"code": 2, "message": "No baseUrl configured. Run: cybersierra auth login --url <url> ..."}}
```

Confirmed by grep across the entire Python codebase: **zero code anywhere
forwarded a base URL to the subprocess.** This repo's own `.env.example`
had a `CYBERSIERRA_BASE_URL` variable whose comment implied it configured
the target backend — but it was never read by any code, never added to the
subprocess env. Dead, decorative config. This had gone unnoticed because
this dev box's own persisted profile already had a correct `baseUrl`
cached in it from a prior manual login — a real deployment would have no
such profile and would fail on the very first request, regardless of how
correct the forwarded per-user token was.

### The fix

`harness/agent.py`'s `_build_agent` now reads `CYBERSIERRA_BASE_URL` and
translates it into `MORPHEUS_BASE_URL`, injected **unconditionally** — not
gated behind `CYBERSIERRA_INJECT_ACCESS_TOKEN` the way `MORPHEUS_TOKEN` is.
This is deliberate: base URL is static, deployment-wide config (the same
value for every user), not per-request secret data — gating it behind the
same toggle as the token would be a category error and could silently
recreate this exact gap for a deployment that sets one but forgets the
other.

```python
base_url = os.environ.get("CYBERSIERRA_BASE_URL")
if base_url:
    env["MORPHEUS_BASE_URL"] = base_url
```

Re-verified live, end to end, through the actual harness code path (a real
`/chat` call, not a manual shell simulation), with a genuinely empty `HOME`
and a placeholder (fake) token:

```
$ HOME=<fresh-empty-dir> CYBERSIERRA_BASE_URL=https://morpheus-api.prod.cybersierra.ai/ \
  CYBERSIERRA_INJECT_ACCESS_TOKEN=1 uvicorn server.app:app
# /chat asked to run `cybersierra auth whoami` with access_token=<placeholder>:
{"error": {"code": 2, "message": "Invalid token"}}
```

A real rejection from the backend — not `"No baseUrl configured"` —
proving base-URL resolution no longer depends on any persisted profile
existing anywhere. A negative control (same fresh `HOME`, base URL
deliberately left unset) correctly reproduces the original failure,
proving the positive result isn't vacuous. Both are now a permanent
regression test: `verify/verify_server_fresh_deployment_no_profile.py`.

The model's own final answer to the user in that same test, completely
unprompted: *"You'll need to sign in again on the CyberSierra platform...
let me know and I can retry."* No CLI names, tokens, or internal mechanics
mentioned — see the next section for why.

## The model can never itself touch auth

`harness/sandbox.py` originally denied only two specific subcommands
(`cybersierra auth login-browser`, `cybersierra auth login`) — added after
a real incident where the model, on hitting an auth error, tried running
`login-browser` itself and burned 7+ minutes on doomed interactive-flow
attempts in a headless sandbox (browser popups can never succeed there).

This was widened to **deny the entire `cybersierra auth` subcommand group
by default**, with exactly one explicit carve-out:

```python
DENIED_COMMAND_PREFIXES: tuple[str, ...] = ("cybersierra auth",)
ALLOWED_DESPITE_DENIED_PREFIXES: tuple[str, ...] = ("cybersierra auth whoami",)
```

Why the whole group, not just enumerating more bad subcommands
(`poll`/`set-token`/`logout`):

- `poll` is the second half of the same doomed interactive flow.
- `set-token`/`logout` aren't interactive, but they mutate or delete the
  one shared, on-disk, process-wide `~/.cybersierra/config.json` — a model
  invoking either would corrupt or destroy every other concurrent or
  future request's identity on the same host, not just its own.
- Deny-by-default means any **future** `cybersierra auth <new-subcommand>`
  the real CLI ships is safe by construction — nothing to remember to
  update.

`cybersierra auth whoami` (read-only, no side effects) is the one explicit
exception, used successfully throughout this harness's own logs as the
model's diagnostic of first resort.

Additionally, the system prompt (`harness/agent.py`'s
`SYSTEM_PROMPT_APPENDIX`) now explicitly instructs: if a command's output
indicates an invalid/expired/unauthorized credential, tell the user in
plain language to sign in again on the CyberSierra platform — never
mention any command name, environment variable, token, or other internal
authentication mechanism. This is prompt-only (not a deterministic
code-level intercept of the CLI's raw error text) — see
[known-limitations.md](known-limitations.md) for why that's an acceptable,
disclosed scope call for v2.

## What this means for a real deployment, concretely

Set three things on this service, and nothing else, ever:

| Env var | What it does |
|---|---|
| `DEEPAGENT_SERVICE_AUTH` | Shared secret; must match morpheus_backend's own value of the same name. |
| `CYBERSIERRA_INJECT_ACCESS_TOKEN=1` | Turns on per-request identity forwarding. |
| `CYBERSIERRA_BASE_URL` | The real backend this deployment targets. Translated to `MORPHEUS_BASE_URL`, injected unconditionally. |

No `cybersierra auth login-browser`, no persisted profile, no interactive
step of any kind, on any host, ever — including a server that's never had
a human touch it. Full env var reference across all three repos:
[deployment-config.md](deployment-config.md).
