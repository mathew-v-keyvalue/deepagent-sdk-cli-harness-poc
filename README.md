# cybersierra CLI + DeepAgents SDK harness — parity POC

The DeepAgents counterpart to the sibling
[`claude-sdk-cli-poc`](../claude-sdk-cli-poc) repo: same shape, same SSE
contract, same verification rigor, built against LangChain's **DeepAgents
SDK** instead of the **Claude Agent SDK**, so the two can be compared head
to head. Where they differ — and they do, in ways worth knowing before
picking one — is called out explicitly below, not smoothed over.

**The rule this POC is built around, same as the sibling:** the skill is
the unit of work. `harness/agent.py` knows nothing about cybersierra — no
command names, no endpoints, no auth flow. Skill discovery does the
routing. Adding skill #2 means adding a new directory under `skills/`; it
does not mean touching the harness.

**One real difference in scope from the sibling POC:** that repo built a
small placeholder CLI (`morpheus`) just to have something to sandbox
against. This one sandboxes the **real** product CLI —
[`@cybersierra/cybersierra-cli`](https://www.npmjs.com/package/@cybersierra/cybersierra-cli)
— and ports the **real, already-built** `cyber-sierra` skill pipeline
(copied verbatim into `skills/cyber-sierra/` from the actual plugin at
`plugins/cyber-sierra/skills/cyber-sierra/` — see "Porting the real
pipeline" below), not a toy example.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs `deepagents`, `langchain-openai` (the alt-provider demo —
`langchain-anthropic` already comes in as a `deepagents` transitive
dependency), `fastapi`, `uvicorn`, `python-dotenv`, `httpx`.

You also need the real `cybersierra` CLI on `PATH`:

```bash
npm install -g @cybersierra/cybersierra-cli
cybersierra auth login-browser --url https://morpheus-api.prod.cybersierra.ai/
```

That login step matters and is explained in full under "Authentication
model" below — short version: this harness does **not** perform login
itself, on purpose.

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY (default provider) and/or OPENAI_API_KEY
```

## Run the harness demo

```bash
python -m harness.agent "Who are you and what can you help me with?"
```

Same shape as the sibling POC's `python -m harness.agent`: `run()` always
takes `access_token` as an explicit argument; the `__main__` block reading
`CYBERSIERRA_DEMO_TOKEN` (defaulting to a placeholder) is that block's own
convenience, not something `run()` does.

## Chat server

```bash
uvicorn server.app:app --reload
```

`POST /chat` — same three form fields and five SSE event names as the
sibling POC. One deliberate, disclosed contract deviation:
**`access_token` is no longer required** (`400 no_token` was dropped) —
see "Authentication model" below for why. Everything else matches:

| Event / status | Shape |
|---|---|
| `event: session` | `{"session_id": str}` (turn 1 only) |
| `event: delta` | `{"text": str}` |
| `event: tool_use` | `{"name": str}` |
| `event: done` | `{"session_id", "subtype", "total_cost_usd", "usage", "num_turns"}` |
| `event: error` | `{"code", "message"}` |
| `404` | `{"error": {"code": "unknown_session", ...}}` |
| `409` | `{"error": {"code": "session_busy", ...}}` |

Verified for real, without needing a model key at all (`unknown_session`
returns before the harness ever runs) — and, separately, with a real live
model call showing `/chat` now works with **no `access_token` field sent
at all**:

```bash
curl -s -X POST localhost:8000/chat -F "message=hi" -F "session_id=nope"
# {"error":{"code":"unknown_session","message":"no session with id 'nope' on this server"}} — HTTP 404

curl -sN -X POST localhost:8000/chat -F "message=Say hi in exactly two words."
# event: session
# data: {"session_id": "0324645d-..."}
# event: delta
# data: {"text": "Hello"}
# ...
# event: done
# data: {"session_id": "0324645d-...", "subtype": "success", ...}
```

Also verified without a model key: a turn against a server with no
`ANTHROPIC_API_KEY` set streams a real `session` event and then a graceful
`error` event (`code: "harness_error"`) — not a crash, not a hang. See
"Known limitations" for exactly what streaming/session behavior still
needs a live model key to confirm, since this session didn't have one.

### Frontend

`frontend/index.html` started as an unmodified copy of the sibling POC's —
proof, at the time, that this harness matched the sibling's SSE contract
exactly with zero frontend changes needed. It's since had one small,
deliberate edit, once the contract itself deliberately diverged (see
"Chat server" above and "Authentication model" below): the token input's
`required`-by-JS check was removed, its placeholder text updated to say
it's optional, and the page title/header text changed from "morpheus" to
"cybersierra" (cosmetic, unrelated to the contract). Everything about how
it consumes the five SSE events is untouched. If a future contract change
needs more than this, this section will say so rather than silently
patching around it.

```bash
uvicorn server.app:app --reload
# open http://localhost:8000/
```

## Verification

Six scripts, run from the project root with the venv active.

### 1. Shell sandbox actually denies

```bash
python verify/verify_shell_sandbox_denies.py
```

**Run in this session — passes.** No model key needed (see the script's
own docstring for why a scripted, not live, model is the honest choice
here). Checks both enforcement layers independently — see "Security
boundary" below for what this actually proved, including a real bug it
caught mid-build (`awrap_tool_call` vs `wrap_tool_call`).

### 2. Chat server: session context carries across requests

```bash
python verify/verify_server_session_context.py
```

Needs a real model key — **not run to completion in this session** (no
`ANTHROPIC_API_KEY` was available; see "Known limitations"). "My name is
Ada Lovelace" / "What is my name?" across two turns on the same
`session_id`, same as the sibling POC's identically-named script. No real
cybersierra token needed — deliberately a plain conversational exchange, so
it isolates the checkpointer/`thread_id` mechanism from anything
cybersierra-specific.

### 3. Chat server: concurrent sessions stay isolated

```bash
python verify/verify_server_multi_session_isolation.py
```

Needs a real model key — **not run to completion in this session.** Two
checks, both always runnable without any real cybersierra credentials
(unlike the sibling POC's `verify_multi_user_isolation.py`, which needs two
real distinct backend tokens and was itself not run to completion there
either):

- **Env-token isolation**, without needing real cybersierra accounts: two
  concurrent sessions, two distinct marker strings as `access_token`, each
  asked to run `python3 -c "...os.environ.get('CYBERSIERRA_TOKEN')..."`
  via the sandboxed `execute` tool and report exactly what it printed.
  Asserts each session's answer contains only its own marker. `python3` is
  itself on the shell allowlist, which is what makes this fully
  automatable — the harness's own env-scoping is what's under test, not
  cybersierra's backend. This check launches its own server with
  `CYBERSIERRA_INJECT_ACCESS_TOKEN=1` (see "Authentication model" below —
  injection is opt-in, off by default) specifically so there's something
  in `CYBERSIERRA_TOKEN` to observe at all.
- **Conversation-state isolation**: same placeholder token, two different
  concurrent `session_id`s told "red" and "blue," a follow-up on each
  proving neither leaked — same design as the sibling POC's identically
  purposed check.

### 4. Chat server: streaming is actually incremental

```bash
python verify/verify_server_streaming_incremental.py
```

Needs a real model key — **not run to completion in this session.** Times
`delta` events for a long response, asserts they're spread over real
wall-clock time (same `MIN_SPREAD_SECONDS = 0.3` design as the sibling
POC — not just "more than one event," which a buffered-then-burst response
would also satisfy). Also asserts a real `tool_use` event fires with this
harness's actual tool name (`execute`, not `Bash`).

### Running the live-model scripts yourself

```bash
export ANTHROPIC_API_KEY=...   # or set AGENT_MODEL=openai:gpt-5.1 and OPENAI_API_KEY=...
python verify/verify_server_session_context.py
python verify/verify_server_multi_session_isolation.py
python verify/verify_server_streaming_incremental.py
```

### 6. Query dataset drift check

```bash
python verify/validate_query_dataset.py
```

`dataset/query_dataset.json` is 51 sample `/chat` queries — one per
**read-only** `cybersierra` manifest command (32; the manifest's other 5
commands are writes and were deliberately excluded — see `dataset/README.md`
"Write operations were removed," since `dataset/run_dataset.py` calls a
real backend and a write example query would mean every dataset run
actually creates/sends/submits/updates real data), plus 14 multi-step
(4 of which chain 3 real commands, not just 2) and 5 deliberately
unanswerable ones. See `dataset/README.md` for how it was
built, and two real behavior gaps it caught by actually running it against
a live server: the model guessing wrong CLI subcommands instead of reading
the loaded skill first (fixed in `harness/agent.py`'s
`SYSTEM_PROMPT_APPENDIX`), and health/notifications-style queries never
triggering the skill at all, because the real `SKILL.md`'s own frontmatter
`description` doesn't mention those domains as trigger words (left as-is —
fixing it means editing the ported skill file, a disclosed exception to
"copied verbatim" that wasn't made unilaterally). `validate_query_dataset.py`
re-pulls the live manifest and enforces two things: no dataset entry
drifted (command gone, or `safe` flag changed), and no dataset entry is a
write operation at all — the latter checked by actually injecting a fake
write-op entry in this session and confirming the script caught it. No
model key needed. Run in this session — passes.

`dataset/run_dataset.py` runs the whole dataset (or a subset) against a
real, already-running `/chat` endpoint and writes the question/answer
pairs — full answer text, every tool called, token usage, timing — to
`dataset/results.json`, with `--resume` support for a long run. This is
what actually caught both gaps above; see `dataset/README.md` for exact
usage and the full before/after evidence.

## Porting the real pipeline

`skills/cyber-sierra/` is a **verbatim copy** of the real plugin skill at
`plugins/cyber-sierra/skills/cyber-sierra/` — `SKILL.md`, `_internal/`
(shared schemas, the Planner, the Reflection/Learning component), and the
starting-empty `_generated/index.json`. Nothing in those files was edited.
This is deliberate: those files are primary source, the same way the
sibling POC's `morpheus` skill was written directly against the real
`morpheus user info` command rather than a paraphrase of it.

**How the 7-phase pipeline (Initialize → Route → Plan → Present-Plan-&-
Confirm → Execute → Reflect-&-Learn → Report) maps onto DeepAgents,** and
why, checked against the installed package rather than assumed:

- **Skill discovery uses DeepAgents' own `SkillsMiddleware`, not a custom
  loader.** Read directly from the installed package
  (`deepagents/middleware/skills.py`): it implements the same [Agent
  Skills spec](https://agentskills.io/specification) as Claude Code —
  `SKILL.md` with YAML frontmatter, progressive disclosure (name +
  description shown up front, full content read on demand via
  `read_file`). Pointing `skills=["skills/"]` at this repo's `skills/`
  directory is enough for the real `cyber-sierra` SKILL.md to be
  discovered and loaded exactly as designed — no adapter code needed for
  this part.
- **No subagent per pipeline component**, despite that being a reasonable
  first guess (and one worth naming, since the prompt this POC was built
  from suggested it as "a natural fit"). The real skill files were
  authored for a *single, continuously-reasoning agent* progressively
  reading `_internal/planner/SKILL.md`, `_internal/reflection/SKILL.md`,
  etc. as the Orchestration Protocol calls for them — not for
  agent-to-agent delegation. `deepagents.SubAgent` (confirmed via
  `inspect.getsource` against the installed package) is a real, available
  primitive — isolated-mode delegation with its own tools/model/prompt —
  and would be a reasonable next step for isolating Planner or Reflection
  specifically. It isn't used here because the more faithful port of *this
  specific* pipeline is one agent reading files progressively, matching
  how the source system actually runs inside Claude Code.
- **The Executor is a plain Python tool
  (`harness/executor_tool.py:run_execution_plan`), not a subagent or a
  prompted step.** `_internal/shared/contracts.md` itself calls the
  Executor "deterministic runtime logic" — we took that literally.
  Resolving `{{inputs.*}}`/`{{step[N].*}}` references, building the CLI
  invocation, and halting on the first non-zero exit code (per
  `contracts.md`'s exit-code table) is exactly the kind of mechanical,
  safety-relevant logic we didn't want an LLM improvising, since a
  hallucinated argument or a skipped halt-on-failure here means a real CLI
  command runs with the wrong input. Verified directly against the real
  `cybersierra` CLI (unit tests in-session; see the script's own
  docstring) — halt-on-first-failure, `{{step[N].*}}` resolution, and
  clean (non-crashing) handling of an unresolved `{{inputs.*}}` reference
  all checked against real CLI output, not mocked.
- **`Present Plan & Confirm` is an ordinary conversational turn, not
  `interrupt_on`.** DeepAgents/LangGraph's `interrupt_on` (human-in-the-
  loop, pausing mid-tool-call, requiring a checkpointer) is a genuinely
  stronger mechanism than what's built here — and notably, it's exactly
  what a sibling decision doc (`AGENT_RUNTIME_DECISION.md`) flagged as
  DeepAgents' distinguishing strength for R4's approval-gate work
  specifically. It isn't used for this step because wiring it in would
  need a new SSE event type ("awaiting approval") that the fixed contract
  this POC must match doesn't have. Instead, the model presents the plan
  in ordinary text and stops (an ordinary `done` event); the user's next
  `/chat` call on the same `session_id` is their approval or rejection,
  read back in via the checkpointer the same way any other turn is. This
  is also, incidentally, closer to how the real skill's own Orchestration
  Protocol describes the step — "Ask for confirmation. If the user
  declines, halt" reads as a conversational instruction in the source
  material too, not a hard runtime block. Like the source system, this
  gate is soft: nothing stops the model from calling
  `run_execution_plan` without asking first except its own instructions.
  `interrupt_on` remains available and would be the right primitive for
  building an actual hard gate — see "Known limitations."

**What this means for R4 (agent self-extension) specifically.** The
sibling POC's decision docs (`AGENT_RUNTIME_DECISION.md`,
`AGENT_CONSOLIDATED_DECISION.md`) call R4 unsolved by any SDK choice —
skills need to become "tenant-scoped, versioned rows in the database," with
"a human must approve it before it is persisted as executable — full stop."
The real `cyber-sierra` pipeline, ported here faithfully, is R4 exactly as
it exists **today**: plain file writes (`_generated/<name>/SKILL.md` +
`metadata.json`, appended to `_generated/index.json`), no tenant scoping,
and — notably — **no approval gate on the persist step itself**. The
pipeline's Step 4 ("Present Plan & Confirm") gates *running* commands, not
*learning* a new skill from a successful run afterward; Reflection can
write a new skill to disk with zero human involvement once
`shouldPersist=true`. This port did not add that gate. That gap is
reproduced faithfully here, on purpose — it's a data point for the
comparison (the same gap exists no matter which SDK hosts this pipeline),
not a bug introduced by this port.

## Authentication model

**This is the one place this POC deliberately does not mirror the sibling
POC's design**, because the real CLI's auth model doesn't fit the sibling's
per-request bearer-token pattern.

The sibling POC's harness holds no identity at all: `access_token` is
injected fresh into each SDK subprocess call via `env=`, per request, never
persisted, and the placeholder `morpheus` CLI was built to match that
exactly (`--token-stdin` > env var > `--token`, nothing cached to disk).
The real `cybersierra` CLI is different by design: `cybersierra auth
login-browser` is an interactive, browser-based OAuth-style flow (opens a
browser, polls up to 5 minutes, supports MFA/SSO) that writes a session to
`~/.cybersierra/config.json` (or `$CYBERSIERRA_CONFIG_DIR`), and
`cybersierra auth whoami` / every other command checks that persisted
profile by default.

**Per explicit product direction for this POC:** assume `cybersierra` has
already been authenticated once, out of band, before the server starts — a
human runs `cybersierra auth login-browser` interactively, and every
`/chat` request after that just works against that one already-established
identity. This matches deployment "shape A" (one pod/process genuinely
dedicated to one user for its lifetime) from the sibling POC's own
addendum — under that shape, a one-time interactive login costs nothing new
to engineer.

**What this harness actually does with `access_token`, given that — two
real, sequential fixes, each caught by a direct question, not by testing:**

*Fix 1 — injection became opt-in.* The first version of this harness
*unconditionally* injected `access_token`, per call, as `CYBERSIERRA_TOKEN`
in the sandboxed subprocess's environment, with `access_token` itself
required on every `/chat` request (`400 no_token` if missing) for exact
SSE-contract parity with the sibling POC. That's a real, verified
mechanism (see below) — but making it the *default*, with no real per-user
JWTs to put in the UI's token field, quietly broke the "already logged in
via `cybersierra auth login-browser`" assumption this whole POC is built
on: grepping the installed CLI binary directly
(`~/.cybersierra/bin/cybersierra`) confirms `token: process.env
.CYBERSIERRA_TOKEN ?? i.token` — and JavaScript's `??` only falls back to
`i.token` (the persisted profile) on `null`/`undefined`, not on a wrong
string. So a UI placeholder like `placeholder-token-not-real` — never
intended as a real credential — was overriding an already-working,
already-authenticated profile and getting rejected by the real backend on
every cybersierra call, silently defeating the entire point of the
one-time-login assumption. Fix: injection became opt-in —
`CYBERSIERRA_INJECT_ACCESS_TOKEN` (unset/empty by default; see
`.env.example`), checked in `harness/agent.py`'s `_build_agent`. With it
unset, `CYBERSIERRA_TOKEN` is simply never added to the subprocess env
dict at all, so the CLI's own env resolution falls through to the
persisted profile.

*Fix 2 — `access_token` stopped being required at all.* Even with
injection opt-in, `access_token` was still a required field purely for
contract parity — meaning the UI still forced you to type *something* into
a token box that, by then, did nothing in the default path. Once asked
directly why a token is needed at all when `cybersierra auth login-browser`
already handles auth, the honest answer was: it isn't, for this POC's
scope. `400 no_token` was dropped entirely — `access_token` is now a
genuinely optional `/chat` field (defaults to `""` server-side; see
`server/app.py` and `harness/agent.py`'s `run()`/`stream()`), and
`frontend/index.html`'s token field is no longer required by its own JS
either (see "Frontend" above for the exact edit). Verified live: a real
`/chat` call with no `access_token` field sent at all streams a normal
answer, tool calls included, using the already-authenticated profile — see
"Chat server" above for the captured transcript. `access_token` still
exists as a field, and still does something — see Fix 1 — for anyone who
sets `CYBERSIERRA_INJECT_ACCESS_TOKEN=1` and supplies a real per-user JWT.

Set `CYBERSIERRA_INJECT_ACCESS_TOKEN=1` to switch on the other, also-real
mechanism this harness supports — true per-request token injection,
overriding whatever profile is on disk. Verified empirically in this
session, against the real `prod` backend, with a syntactically-plausible
but fake token:

```
$ env -i CYBERSIERRA_TOKEN="fake.jwt.token" PATH="..." HOME=/tmp/nonexistent-home cybersierra auth whoami
{"error":{"code":1,"message":"connect ECONNREFUSED 127.0.0.1:1"}}   # (fake base URL in this test)
$ env -i PATH="..." HOME=/tmp/nonexistent-home cybersierra auth whoami   # no CYBERSIERRA_TOKEN, no profile
{"error":{"code":2,"message":"No token for profile \"default\". Run: cybersierra auth login"}}
```

The first call attempted a real network call with the injected token (only
failing because the test pointed `CYBERSIERRA_BASE_URL` at an unreachable
address on purpose); the second, with no token anywhere, failed at the
local config-check stage instead — proving the env var is read and
prioritized before the local profile, not silently ignored.

So: **`CYBERSIERRA_TOKEN` injection is the real, verified mechanism a
shape-B/C multi-tenant deployment would need** (per caller, per call,
overriding whatever profile happens to be on disk) — it's built, working,
and covered by `verify/verify_server_multi_session_isolation.py`'s
env-isolation check (which turns the opt-in on for the one server instance
it launches). It isn't this POC's *default* because we have no real
per-user cybersierra JWTs to demonstrate it against, and because turning it
on without one is actively worse than leaving it off, per the bug above.
To exercise real cybersierra command success with injection mode on, pass
the real token from an already-completed `login-browser` session (readable
from `~/.cybersierra/config.json`'s `token` field, or via `auth whoami`) as
`access_token` on your request.

**What `cybersierra` does *not* offer, confirmed by reading `cybersierra
auth --help` and every subcommand's own `--help` directly:** there is no
non-interactive way to *acquire* a first token — `login` (email/password)
and `login-browser` (MFA/SSO) are the only two acquisition paths, and both
are interactive by design (even `login` needs a password prompt or
`$CYBERSIERRA_PASSWORD`, not a token exchange). `set-token <jwt>` registers
an already-acquired token but doesn't get you one. So for a real shape-B/C
deployment serving many users from one process, `CYBERSIERRA_TOKEN`
injection solves the "route the right identity into each call" half of the
problem, but *acquiring* each user's JWT in the first place is still
outside what this CLI does for you — that would need a token-exchange step
built against wherever the platform's own SSO already lives, which this POC
does not attempt.

## Security boundary: how the shell sandbox is actually enforced

DeepAgents' own docs are explicit that its local-execution backend
(`LocalShellBackend`) ships with **no command allowlisting of its own** —
read directly from the installed package's docstring: *"Since shell access
is unrestricted... Enable Human-in-the-Loop (HITL) middleware... STRONGLY
RECOMMENDED as your primary safeguard."* That's not what this harness
needed (a programmatic allowlist, not a human approving every command), so
building the allowlist itself was ours to do — DeepAgents gives the hook
points, not the policy, same as the sibling POC found true of the Claude
SDK.

**Two independent enforcement layers** (`harness/sandbox.py`):

1. **`AllowlistedShellBackend`** — a `LocalShellBackend` subclass that
   checks the command inside the same method that would otherwise call
   `subprocess.run`. This is the layer we'd trust even if layer 2 turned
   out to be silently bypassed the way the sibling POC found `can_use_tool`
   was for `Bash`.
2. **`ShellSandboxMiddleware`** — a `wrap_tool_call`/`awrap_tool_call`
   middleware, the documented DeepAgents/LangChain mechanism for
   intercepting a tool call before it runs. This is the DeepAgents
   counterpart to what the sibling POC's `can_use_tool` callback was
   *supposed* to be.

**What was actually tested, and how** (`verify/verify_shell_sandbox_denies.py`,
run in this session — passes):

- Layer 1: a denied `touch <marker>` really doesn't create the file (a real
  filesystem side-effect check, not just "no exception"), and gets back
  `exit_code=126`. An allowed command (`cybersierra --version`) really
  reaches the real binary.
- Layer 2: run through an **actual compiled DeepAgents graph** (not the
  middleware method called by hand) — a scripted, not live, model
  deterministically attempts a disallowed `execute` call, and the check
  confirms both the real filesystem side-effect (file not created) and the
  returned `ToolMessage.status == "error"` — the DeepAgents analogue of
  inspecting `tool_result.is_error` directly, the same standard the sibling
  POC applied to the Claude SDK.

**A real gap this same direct-instrumentation process caught, mid-build:**
this harness runs the graph asynchronously (`astream_events`/`ainvoke`,
required for real token-level streaming). A first version of
`ShellSandboxMiddleware` implemented only `wrap_tool_call` (sync). Running
it through a real async graph invocation raised
`NotImplementedError: Asynchronous implementation of awrap_tool_call is not
available` — LangChain's async tool dispatch does **not** silently fall
back to a sync-only hook. This is a *louder* failure than the sibling POC's
finding (that one was `can_use_tool` being silently never invoked for
`Bash` — no error, just an empty deny list and unrestricted execution); this
one crashes instead of bypassing. Still, it would have broken this harness
on its first tool call had it shipped, and it's exactly the class of thing
"the docs say this should work" doesn't catch — only running it did. Both
`wrap_tool_call` and `awrap_tool_call` are implemented now, and the verify
script exercises the async path specifically, so this is now a regression
test, not just a war story.

**The real skill's own `allowed-tools` frontmatter line is not an
enforcement mechanism here either** — confirmed by reading
`deepagents/middleware/skills.py` directly: `allowed-tools` (the exact
hyphenated key the real `skills/cyber-sierra/SKILL.md` uses) is parsed and
shown as a descriptive `"-> Allowed tools: ..."` line in the system prompt,
and nothing else — a repo-wide grep for `allowed_tools` across the
installed `deepagents` package turns up zero code paths that use it to gate
a tool call. This is the same lesson the sibling POC learned about
`can_use_tool`/`allowed-tools` not being self-enforcing, restated: true for
both SDKs, not a DeepAgents-specific shortcoming.

**Known gap, same class as the sibling POC's:** both enforcement layers
here do prefix matching on the command string, not shell-grammar parsing.
`cybersierra manifest; rm -rf /` still starts with an allowed prefix and
is **not** caught — the semicolon-joined second command runs too, because
`LocalShellBackend.execute` (like the Claude SDK's own Bash tool) invokes
the whole string through a real shell. Neither POC solves this; it's listed
here rather than silently accepted.

## Proper logging: proof of what happened, not just the chat response

The SSE contract deliberately doesn't surface tool inputs/outputs or which
skill got loaded (`tool_use` only carries `{"name": str}` — see "SSE
contract" above) — that's for the chat UI. Separately from that,
`harness/observability.py:configure_logging()` turns on structured logging
across `harness.*` (attached to the `"harness"` logger; `NullHandler`s
everywhere else keep this silent when embedded in something that
configures its own logging instead) — called automatically by
`server/app.py` on import and by `harness/agent.py`'s own `__main__` demo.
Set `HARNESS_LOG_LEVEL=DEBUG` (default `INFO`) to control verbosity.

**Written to a real file, not just the console.** The first version of
this only ever attached a console (`stderr`) handler — real for the
lifetime of the terminal, gone the moment it closed. `configure_logging()`
now also attaches a `logging.handlers.RotatingFileHandler` writing to
`logs/harness.log` (created automatically; rotates at 10 MiB, keeps 5
backups — `HARNESS_LOG_FILE`, `HARNESS_LOG_MAX_BYTES`,
`HARNESS_LOG_BACKUP_COUNT` override any of that; set `HARNESS_LOG_FILE=""`
to disable file logging and keep only the console). `logs/` is gitignored
— it's runtime output, and while `access_token` is scrubbed out of every
line (see `scrub()` below), CLI output previews and tool args are not, so
treat it the way you'd treat any log file with real backend data in it.
Verified in this session with a real live run: `logs/harness.log` after
one `/chat` turn contains the full `turn_start` → `tool_call_allowed` →
`cli_call_start`/`cli_call_done` (including a wrong-command guess, the
manifest-discovery call, and the eventual correct `cybersierra tprm
assessees count`) → `skills_available` → `turn_done` chain — the same
content the console shows, just persisted.

Every real CLI invocation, every tool call, and which skill got read are
all real log lines, not something the chat UI shows — this is genuinely
the audit trail proving what happened, independent of what the model
*says* happened:

```
2026-09-03 16:23:07 INFO harness.agent   | turn_start thread_id=... prompt='who am I?'
2026-09-03 16:23:07 INFO harness.sandbox | tool_call_allowed name=execute command='cybersierra auth whoami'
2026-09-03 16:23:07 INFO harness.sandbox | cli_call_start command='cybersierra auth whoami'
2026-09-03 16:23:08 INFO harness.sandbox | cli_call_done command='cybersierra auth whoami' exit_code=0 truncated=False output='{"data": {"email": "...", ...}}'
2026-09-03 16:23:08 INFO harness.agent   | skills_available names=['cyber-sierra']
2026-09-03 16:23:08 INFO harness.agent   | turn_done thread_id=... num_turns=1 tool_calls=['execute'] usage={'input_tokens': 3262, ...}
```

(Captured for real, from a live run against this session's `.env`
credentials — not fabricated.)

| Event | Logger | Fires when |
|---|---|---|
| `turn_start` / `turn_done` | `harness.agent` | Every `run()`/`stream()` call — thread id, redacted prompt preview; on `turn_done`, tool names called, token usage |
| `skills_available` | `harness.agent` | After the graph runs once on this thread — every skill `SkillsMiddleware` discovered, by name (see below for why "after," not "before") |
| `skill_load_error` | `harness.agent` | A skill source failed to load (bad frontmatter, missing path, etc.) |
| `tool_call_allowed` / `tool_call_denied` | `harness.sandbox` | Every single tool call the agent makes, from `ShellSandboxMiddleware` — the tool-call-level half of the sandbox's own audit trail |
| `skill_loaded` | `harness.sandbox` | Specifically a `read_file` on a `SKILL.md` — the direct answer to "did the agent actually open this skill," distinct from the broader `skills_available` catalog |
| `cli_call_start` / `cli_call_done` / `cli_call_denied` | `harness.sandbox` | Every real subprocess `AllowlistedShellBackend.execute` attempts — the actual command string, exit code, and (truncated, redacted) output |
| `plan_start` / `plan_step_start` / `plan_step_done` / `plan_done` | `harness.executor` | Every step of a `run_execution_plan` call — ties a `cli_call_*` line back to which Canonical Execution Plan step and `reason` it came from |
| `harness_error` | `harness.agent` | A turn failed — full traceback via `logger.exception` |

**A real ordering bug this caught, in-session, by testing rather than
assuming:** the first version of `skills_available` logging ran *before*
`astream_events`, on the theory that skills are "available" as soon as the
graph is built. Running it that way logged an empty list every time —
`SkillsMiddleware.abefore_agent` (confirmed by reading
`deepagents/middleware/skills.py`) only populates `state["skills_metadata"]`
once the graph actually starts executing on that thread, not at
`create_deep_agent()` time. Moved to after the `astream_events` loop; the
log line above (`skills_available names=['cyber-sierra']`) is from the
fixed version, from a real run.

**Why the returned chat text is never redacted, but log lines are:**
`harness/sandbox.py:scrub()` strips `access_token` out of every
`cli_call_*`/`tool_call_*`/`skill_load_error` log line, but deliberately
*not* out of the `TextDelta`/`Done` content actually streamed back to the
caller. That's on purpose: `verify_server_multi_session_isolation.py`'s
env-isolation check proves per-request token scoping by asking the model
to echo the injected `CYBERSIERRA_TOKEN` back in its answer — a blanket
scrub-everywhere policy (which is what the sibling Claude SDK POC does)
would make that check unable to observe its own result. The caller already
has their own token (they sent it); what this scrub protects against is
that same token ending up in a log aggregator or file with different
access than the original caller, which is a real, distinct concern from
what the caller's own response shows them.

## Model-agnostic by config

`create_deep_agent`'s `model` parameter accepts a `'provider:model-name'`
string passed straight to LangChain's `init_chat_model` — confirmed
directly against the installed packages, not docs prose:

```
>>> init_chat_model('anthropic:claude-sonnet-4-6')
<class 'langchain_anthropic.chat_models.ChatAnthropic'>
>>> init_chat_model('openai:gpt-5.1')
<class 'langchain_openai.chat_models.base.ChatOpenAI'>
```

`harness/model.py:resolve_model()` reads this from `AGENT_MODEL` (default:
Anthropic) — switching providers is an env var, not a code change, which is
exactly the flexibility the sibling POC's own `CLAUDE_SDK_RECONSIDERATION.md`
says Claude Agent SDK doesn't have ("Anthropic only. A LiteLLM multi-vendor
request was closed 'not planned.'"). OpenAI was chosen as the alt-provider
demo per product direction; `langchain-anthropic` and `langchain-openai`
are both installed, so `AGENT_MODEL=openai:gpt-5.1` (with `OPENAI_API_KEY`
set) is enough to run this entire harness — same code path, same sandbox,
same SSE contract — against a non-Anthropic model. This was confirmed to
*resolve* correctly (both provider strings return the right
`BaseChatModel` subclass, with no live API call needed to prove that); a
full live run against OpenAI specifically was not completed in this
session for the same reason the Anthropic live-model verify scripts
weren't — see "Known limitations."

## SSE contract: shape vs content

The event *names* and payload *field names* are exact, by design (that's
what let `frontend/index.html` be copied unmodified — see above). Two
fields inside `done` carry different *content* than the sibling POC's,
worth knowing about even though the shape matches:

- **`total_cost_usd` is always `null` here.** The Claude SDK computes this
  itself (it's Anthropic-specific pricing knowledge baked into the SDK's
  `ResultMessage`). LangChain/DeepAgents doesn't compute a dollar figure at
  all — `usage_metadata` gives token counts, not cost. Rather than
  fabricate a number, this harness always reports `null`.
- **`usage` is a token-count dict in LangChain's standard shape**
  (`input_tokens`/`output_tokens`/`total_tokens`, accumulated across every
  model call this turn made) — the same field name as the Claude SDK's
  `usage`, but not necessarily the same inner keys as Anthropic's raw usage
  object (e.g. `cache_creation_input_tokens`). The SSE contract as
  specified only pins the top-level field names (`session_id`, `subtype`,
  `total_cost_usd`, `usage`, `num_turns`), not the internal shape of
  `usage` itself, so this is within contract — just flagged here rather
  than left for the reader to discover.
- **`subtype` only ever takes two effective values here**: `"success"`
  on a clean turn, or the turn ends as an `error` event instead
  (`code: "harness_error"`) with no `done` event at all. The Claude SDK's
  `ResultMessage.subtype` has a richer vocabulary (e.g.
  `error_max_turns`); this harness doesn't reproduce that granularity.

## Session continuity: checkpointer vs on-disk transcript

Both POCs hold no message-history transcript in their own server code —
`server/sessions.py` here, like the sibling's, is only a
session-id-to-lock/metadata map. Where the actual conversation state lives,
and how a `resume`-style call behaves, genuinely differs:

| | Claude SDK POC | This POC |
|---|---|---|
| Where state lives | The SDK's own on-disk transcript file, opaque format, one per `session_id` | `harness.agent._checkpointer` (an `InMemorySaver`), plain LangGraph state objects, keyed by `thread_id` |
| Process model per turn | A fresh `claude` CLI **subprocess** per call (`session_id=` to create, `resume=` to continue — two different call shapes) | **In-process** — one Python function call (`session_id`/`resume` kwargs are kept for API-shape parity with the sibling POC, but DeepAgents' checkpointer doesn't actually distinguish "new" from "resumed"; a `thread_id` with no prior checkpoint just starts fresh) |
| Behavior on an unknown/expired session | The SDK raises (`ClaudeSDKError`, "No conversation found with session ID..."), which the harness maps to `Failed("session_expired", ...)` | **Silent.** A checkpointer given a `thread_id` it has never seen just starts a brand-new empty conversation — no error, no signal. This POC's `unknown_session` 404 works anyway, but only because `server/sessions.py`'s own dict is checked *before* the harness is ever called — if that dict and the checkpointer's backing store were ever two different, independently-restartable stores (not true in this single-process POC, but would be true in a persistent-checkpointer deployment), this harness would quietly resume nothing and look like it worked while actually starting over. |
| Storage this POC ships with | — | `InMemorySaver`: process-lifetime, wiped on restart — same deliberate POC-scope choice as the sibling's `SessionStore`, and DeepAgents/LangGraph ships persistent alternatives (Postgres, SQLite checkpointers) as a swap-in, not a rewrite, when that scope needs to change |

The practical upshot for the SDK comparison: DeepAgents' checkpointer is
strictly less "load-bearing infrastructure per session" (no subprocess,
no ~1 GiB floor, no on-disk transcript file to manage) — but the silent-
resume-into-nothing behavior above is a real property difference, not a
strict improvement, and worth testing explicitly before relying on it in
anything past POC stage.

## Adding a new skill

Add a new directory under `skills/` with its own `SKILL.md`. `harness/
agent.py` does not change — `SkillsMiddleware(sources=["skills/"])`
discovers every top-level skill directory automatically, the same
`skills="all"`-style behavior the sibling POC relies on for
`.claude/skills/`.

The one thing every new skill inherits from the harness, not its own
`SKILL.md`: shell access is sandboxed to the prefixes in
`harness/sandbox.py:ALLOWED_COMMAND_PREFIXES`. If a future skill needs to
shell out to something not already on that list, that's a harness change,
not a skill change — flag it rather than working around it.

## Known limitations

- **The three formal live-model verify scripts
  (`verify_server_session_context.py`,
  `verify_server_multi_session_isolation.py`,
  `verify_server_streaming_incremental.py`) still haven't been run to
  completion as scripts** in this session, even though a real
  `ANTHROPIC_API_KEY` is now present in `.env` (it wasn't when this was
  first written) and has been exercised live via ad hoc `curl` calls —
  see "Chat server" above for a captured real transcript, including a real
  `cybersierra` tool call succeeding with no `access_token` sent at all.
  Run the three scripts yourself per "Running the live-model scripts
  yourself" above before relying specifically on the
  streaming-is-incremental/session-carries-context/concurrent-isolation
  claims they check — ad hoc spot checks aren't a substitute for what
  those scripts assert precisely (timing spread, exact recall, no
  cross-talk).
- **The `404 unknown_session` error path was verified live; `400 no_token`
  no longer exists** — `access_token` was made fully optional (see
  "Authentication model"), so there's no missing-token case left to
  return `400` for.
- **The OpenAI alt-provider path was confirmed to *resolve* correctly**
  (`init_chat_model('openai:...')` returns a real `ChatOpenAI` instance,
  checked against the installed package) **but was not run live** for the
  same reason above — no `OPENAI_API_KEY` was available in this session.
- **Command-prefix sandboxing doesn't parse shell grammar** — see
  "Security boundary" above. Same gap class in both POCs.
- **`run_execution_plan`'s CLI-flag construction is a simplification**: it
  doesn't consult the manifest's per-parameter `in` (path/query/body/file)
  metadata, since that isn't threaded through the `ExecutionPlan` schema —
  every argument becomes `--name value` except the two names
  `manifest-usage.md` calls out specially (`data` → `--data '<json>'`,
  `file` → `--file <path>`). Fine for the read-heavy commands this POC
  plans against; a param-location-aware version would need the manifest
  passed alongside the plan.
- **`Present Plan & Confirm` has no hard runtime gate** — see "Porting the
  real pipeline" above. Nothing stops the model from calling
  `run_execution_plan` without asking first except its own system-prompt
  instructions. This matches the real skill's own design (a soft,
  instruction-based gate), not a weakening introduced by this port — but
  it's also exactly the kind of gate `AGENT_RUNTIME_DECISION.md`'s D4 says
  R4 needs to be hard, "enforced," with "no author-and-immediately-execute
  path." `interrupt_on` is the primitive that would make it hard; wiring it
  in was out of scope here because of the SSE-contract conflict explained
  above, not because it isn't available.
- **R4's DB-backed, tenant-scoped, approval-gated skill store is not
  built here, on purpose** — see "Porting the real pipeline." The gap is
  reproduced faithfully from the real system, not papered over, and not
  solved — consistent with the sibling POC's decision docs calling this
  unsolved "regardless of SDK."
- **`session_busy` (409) is implemented but not covered by a dedicated
  verify script** — neither is it in the sibling POC's five scripts. It was
  spot-checked manually in this session: without a real model key, a
  turn fails near-instantly (missing API key), leaving no real window to
  observe the lock being held, so a meaningful automated check needs a
  live model call slow enough to overlap with a second request. Confirm
  this yourself once you have a live key: fire two `/chat` calls on the
  same `session_id` back-to-back and confirm the second gets `409` while
  the first is still streaming.
- **The `409` check-then-acquire in `server/app.py` is race-free only
  under a single worker/event loop** — identical caveat to the sibling
  POC, for the identical reason (no `await` between the check and the
  acquire).
- **`server/sessions.py` and `harness.agent._checkpointer` are both plain
  in-process stores** — no TTL/eviction, lost on restart, don't work
  across multiple server processes/workers. Deliberate POC scope, same as
  the sibling's `SessionStore`.
- **The silent-resume-into-nothing checkpointer behavior** described under
  "Session continuity" above was reasoned from reading `langgraph`'s
  checkpointer source and confirmed for the in-memory case used here; it
  was not separately tested against a persistent (Postgres/SQLite)
  checkpointer, which this POC doesn't use.
# deepagent-sdk-cli-harness
# deepagent-sdk-cli-harness
