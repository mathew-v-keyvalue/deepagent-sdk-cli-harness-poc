# The eval framework

Two independent tracks, one CLI:

```bash
python -m eval run --target local              # no cost, no Netra, ready today
python -m eval run --target local --gate       # + confirm-gate pause-only check
python -m eval run --target netra              # direct-service Netra track (see below)
python -m eval run --target both               # local, then netra
```

## Folder structure

```
eval/
├── __main__.py              # this CLI
├── shared/chat_client.py    # SSE parsing + /chat POST, shared by local + gate
├── local/
│   ├── dataset.json         # 53 read-only scenarios (32 single, 14 composite, 5 adversarial, 2 auth)
│   ├── gate_scenarios.json  # 5 write-shaped scenarios, deliberately safe:false
│   ├── runner.py             # calls /chat, writes results.json
│   ├── gate_runner.py         # confirm-gate pause-only check
│   └── scoring.py / score.py  # precision/recall/F1 + auth leak-check, offline
├── netra/
│   ├── dataset.json          # independent Netra-track dataset (51 items, all read-only)
│   ├── setup_dataset.py      # creates/syncs the Netra dataset via SDK
│   ├── task.py / run.py       # in-process run_test_suite() against harness.agent.run()
│   ├── run_via_production_hop.py  # PARKED — see "Two things both called 'the Netra track'"
│   └── EVALUATOR_SETUP.md    # one-time manual dashboard steps
├── CLI_COMMAND_REFERENCE.md   # cybersierra CLI reference doc
├── verify_datasets.py         # drift check — both invariants, all three dataset files
└── results/                   # gitignored — run outputs, Netra ids, score summaries
```

## Two independent dataset definitions

`eval/local/dataset.json` and `eval/netra/dataset.json` are deliberately
**not** the same file, and the Netra one is not generated from the local
one at runtime — a separate, authored copy (the Netra one is a 51-item
mirror of the local dataset's read-only scenarios, scaled up from an
initial 13-item smoke subset once the pipeline was confirmed working
end-to-end). This is a deliberate choice: it lets each track's dataset
evolve independently without one script silently changing what the other
scores, at the cost of the two occasionally drifting out of sync with each
other (they don't drift from the *live CLI*, though — see "Keeping this
current" below, which checks both files against it).

## Two things both called "the Netra track"

Do not conflate these — they answer genuinely different questions and have
different blockers:

| | Tests | Needs | Status |
|---|---|---|---|
| **`eval/netra/run.py`** (2b — the one this framework builds for) | Does the agent behave correctly, with results visible via Netra's library evaluators? | `NETRA_API_KEY`/`NETRA_OTLP_ENDPOINT`/`NETRA_TRACING` only | Ready today |
| **`eval/netra/run_via_production_hop.py`** (2a — parked) | Does the *real production wiring* (frontend → morpheus_backend → this service) also work? | `MORPHEUS_BACKEND_URL` + a dedicated eval tenant (MFA disabled) | **Blocked** — nobody has provisioned this; it's a backend/devops ask, not an AI-engineering one, since it's testing cross-service plumbing, not agent behavior |

`eval/netra/run.py` calls `harness.agent.run()` directly, in-process — no
running server, no HTTP hop, no eval tenant. It still produces real
`Agent_Turn`/`Plan_Step`/`CLI_Call` spans (via the same `Netra.init()` call
`harness/tracing.py`'s `init_tracing()` already makes for the live server,
just under a distinct `app_name` so eval traces stay visually separable in
the dashboard), so Tool Correctness still has real `trace.tools` data to
score.

### The Netra SDK has no evaluator-creation API

Checked directly against the installed `netra-sdk` — first v0.1.98, then
re-checked against v1.0.1 after upgrading (see "A real SDK bug, found and
fixed" below) since a major version bump was a reasonable place for this
gap to have been closed too. It wasn't: v1.0.1 can create datasets, dataset
items, and test runs, and score *your own* custom Python `BaseEvaluator`
subclasses locally — but there is still **no** API for creating or
attaching Netra's built-in library evaluators (Answer Relevance, Tool
Correctness, Toxicity, etc.). That's exclusively an MCP-tool or
dashboard-UI capability. This Netra instance doesn't support MCP (Dynamic
Client Registration rejected — `Cannot POST /register`), so the one-time
evaluator setup is a manual dashboard step — see
`eval/netra/EVALUATOR_SETUP.md`.

### A real SDK bug, found and fixed

`netra-sdk` 0.1.98's `create_dataset()` never sent the `datasetType` field
the live Netra backend requires — confirmed live: a 400 without it, a 201
once it's added. Not something fixable in our code; it's the SDK's own
public API missing a required field entirely. `netra-sdk` 1.0.1 fixes this
properly (`create_dataset(..., dataset_type=DatasetType.TEXT)`). Checked
every API surface both this eval framework and the live harness's tracing
depend on (`Netra.init`, `start_span`, `set_session_id`, `set_root_input`/
`set_root_output`, `InstrumentSet.FASTAPI`/`LANGCHAIN`, `SpanType`,
`DatasetItem`/`TurnType`/`Dataset`) before upgrading — all compatible,
confirmed by importing every `harness.*` and `eval.*` module cleanly after
the bump. **If you run the live server as a separate long-running process,
restart it after this upgrade** — `uvicorn --reload` only watches source
files, not installed dependency versions, so an already-running process
keeps whatever `netra-sdk` version was loaded at its own startup.

## Telling eval traffic apart from real frontend traffic in Netra

`eval/netra/run.py` is already separable (distinct `app_name`, see above).
But `eval/local/runner.py`/`gate_runner.py` call this service's `/chat`
directly, on the **same** running server process, under the **same**
`app_name` as real end-user traffic (`morpheus_fe` → `morpheus_backend`'s
`/tracy/chat` proxy → this same `/chat`) — with no way to tell the two
apart in the Netra dashboard, since nothing distinguished them.

The fix needed no harness/server changes: `harness/agent.py`'s `stream()`
(what `/chat` actually calls) does `Netra.set_session_id(thread_id)` where
`thread_id` is exactly whatever `session_id` the HTTP caller sent — so the
eval runners now mint their own recognizably-prefixed `session_id` per
query instead of letting the server auto-generate an opaque one:

- `eval/local/runner.py` → `eval-local-{category}-{dataset_id}-{6 hex chars}`
- `eval/local/gate_runner.py` → `eval-gate-{scenario_id}-{6 hex chars}`

Real frontend sessions use `morpheus_fe`'s own UUID scheme, which never has
an `eval-` prefix — so in the Netra dashboard's session list, anything
starting with `eval-` is unambiguously this framework's traffic, filterable
at a glance, no per-trace inspection needed.

## Confirm-gate scope

The skill's Step 4 "Present Plan & Confirm" gate (before write commands) is
**soft** — the model decides, based on the skill's own instructions, to
present a plan and ask before writing; there's no separate "awaiting
confirmation" signal in the API, just the model's text response. Testing
it fully (send write query → confirm → verify the write executed) means a
**real write actually happens** against whatever tenant `cybersierra` is
pointed at — the same real-side-effect risk that got the 5 write commands
excluded from `eval/local/dataset.json` in the first place.

So confirm-gate coverage is split in two:

- **`eval/local/gate_runner.py`** (built, ready today): sends each
  `eval/local/gate_scenarios.json` write-shaped query as a single turn and
  checks the gate paused — zero write-command tool_calls invoked, and the
  response text reads as asking for confirmation. Zero real side effects,
  no tenant needed.
- **Full confirm→execute→verify-write-happened test** (parked): needs a
  dedicated write-capable test tenant, same class of ask as the
  production-hop track above.

## Auth-flow coverage

`eval/local/dataset.json`'s `auth_scenarios` (2 entries) are the only
entries with an *explicit, fixed* `access_token`. Every other scenario now
gets a real one too, if available — see "Real per-request identity" below —
matching the harness's own "empty access_token = no per-request identity"
default (`harness/agent.py`) only when no eval tenant is configured at all.

- `auth-valid-token`: proves per-user token forwarding
  (`CYBERSIERRA_INJECT_ACCESS_TOKEN`/`MORPHEUS_TOKEN`, see
  `poc-wiki/v2/auth-flow.md`) produces a correct, real answer. Its
  placeholder `access_token` is auto-resolved at runtime (see below) if an
  eval tenant is configured — no manual editing needed in that case.
- `auth-invalid-token`: a deliberately garbage token, safe to run as-is —
  the command still gets invoked (the backend rejects the token, not the
  harness), so what's actually scored differently is the final answer
  text, via `eval/local/scoring.py`'s `check_no_internal_leakage`: must
  read as a plain-language "sign in again" message, must never name
  `MORPHEUS_TOKEN`/`cybersierra`/internal env vars.

## Real per-request identity for every query

If `EVAL_MORPHEUS_EMAIL`/`PASSWORD`/`ORG` and `CYBERSIERRA_BASE_URL` are all
set (a dedicated eval tenant — see `.env.example`), `eval/shared/
eval_identity.py` logs in once per run via `cybersierra auth login`
(non-interactive — distinct from `login-browser`, which the sandbox denies
the *model* from running, but has no bearing on this script running it
directly) and attaches the resulting real JWT as `access_token` on every
query that doesn't already carry its own explicit one. This is what
actually gets past "you need to sign in again" on a host with no persisted
CLI profile — a real identity, not just a routed command, matters here
because the skill's Step 4 confirm-and-execute flow needs an authenticated
session to complete at all.

Without an eval tenant configured, behavior is unchanged from before: no
`access_token` sent, falling back to whatever's in the server host's
default persisted profile (`~/.cybersierra/config.json`), if anything.

---

# `eval/local/dataset.json`

A set of sample `/chat` queries derived directly from the real
`cybersierra` CLI's manifest — not invented independently. It exists to
answer "what can I actually ask this harness?" with real examples grounded
in real backend capability, and to give the ported `cyber-sierra` pipeline
(Router → Skill Resolver/Planner → Executor → Reflection) something
realistic to be tested against.

## How it was built

1. Ran `cybersierra manifest --raw` and `cybersierra manifest` to get the
   full, current command catalog (37 commands across `health`,
   `notifications`, `permissions`, `tprm`, `user-management`, as of manifest
   version `367f09699890` — see the file's own `generated_from` block).
2. Read every command's `description`, `params`, and `safe` flag directly
   off that output — no guessing at what a command does from its name
   alone (`tprm assessments verification`'s actual purpose, for example, is
   only clear from its description: "verified questions," not "verify an
   assessment").
3. Wrote one or two natural-language example queries per **read-only**
   command, the way a compliance/vendor-risk user would actually phrase a
   request (matching `skills/cyber-sierra/SKILL.md`'s stated domain), not
   CLI-flag syntax. The 5 write commands (`tprm assessees create`, `tprm
   assessments send`/`submit`/`update`, `tprm risk-score-config
   update-org`) were deliberately left out — see "Write operations were
   removed" below.
4. Added composite queries that require chaining 2+ commands — including
   ones that need a `{{step[N].*}}` reference (per
   `_internal/planner/references/execution-plan-schema.md`), since a
   dataset of only single-command queries would never exercise the
   Planner's actual dependency-ordering logic.
5. Added adversarial queries with **no matching manifest command** —
   `planning-rules.md`'s "No hallucinated commands" rejection rule needs
   something to actually reject, or it's untested.
6. Added `auth_scenarios` (see "Auth-flow coverage" above) — the one
   category that forwards a per-request `access_token`.

## Contents

- `single_command_queries` (32) — one entry per **read-only**
  (`safe:true`) manifest command: `module`/`resource`/`action`, the exact
  `command` string, `safe`, `required_params`, and 1–3 `example_queries`.
  The manifest actually has 37 commands (5 are writes) — see "Write
  operations were removed" below.
- `composite_queries` (14) — a natural request plus the `expected_commands`
  sequence a correct Plan should produce (all read-only); 4 of the 14 chain
  3 real commands rather than 2.
- `adversarial_queries` (5) — requests with no real capability behind them,
  plus why they should be rejected rather than answered with an invented
  command.
- `auth_scenarios` (2) — see "Auth-flow coverage" above.

## Write operations were removed

The 5 `safe:false` manifest commands (`tprm assessees create`, `tprm
assessments send`/`submit`/`update`, `tprm risk-score-config update-org`)
were removed from this dataset entirely, not just left unexercised.
`eval/local/runner.py` calls the real `/chat` endpoint against a real
`cybersierra` backend — a `safe:false` example query in the dataset means
running the dataset **actually creates a vendor, sends/submits/updates a
real assessment, or changes org-level risk scoring** in whatever tenant
the server's `cybersierra` CLI is authenticated against, every single run.
That's a real, repeatable side effect on real data, not a hypothetical —
worth removing outright rather than trusting every future run to remember
not to include them.

`eval/verify_datasets.py` enforces this as a hard, ongoing invariant, not
just a one-time cleanup: it fails if any `eval/local/dataset.json` or
`eval/netra/dataset.json` entry is `safe:false`, and separately fails if a
currently-`safe:true` command's live `safe` flag ever flips to `false` (a
read endpoint gaining a destructive side effect in a future CLI update) —
checked, in the session that built this invariant, by injecting a fake
write-op entry and confirming the script caught it before restoring the
clean dataset. The same script also enforces the *inverse* invariant on
`eval/local/gate_scenarios.json` — see "Confirm-gate scope" above.

If you want to specifically exercise the Present-Plan-&-Confirm gate
end-to-end (not just the pause-only check `--gate` already runs), do that
by hand against the running server with a prompt like "Add a new vendor,"
rather than through this dataset, `eval/local/runner.py`, or
`eval/local/gate_runner.py`.

## Running the whole dataset against a real server

```bash
uvicorn server.app:app &                    # start the server yourself first
python -m eval.local.runner                 # defaults: localhost:8000, one example
                                             # query per entry, all four categories
```

Writes `eval/results/results.json` (gitignored — it's a run's output, not
the dataset itself, and may contain real data from your cybersierra
tenant): one entry per query with the model's full answer, every tool it
called, token usage, timing, and the `expected` command(s)/behavior from
`dataset.json` alongside it for comparison. `--resume` skips queries
already answered in an existing output file; `--all-examples` runs every
`example_queries[]` variant instead of just the first; `--categories
single,composite,adversarial,auth` and `--limit N` narrow a run down. See
`python -m eval.local.runner --help` for the rest.

This calls the real HTTP `/chat` endpoint (SSE parsing included), not
`harness.agent.run()` directly — it's exercising the actual exposed API
surface, same as a real client would.

## Using it

**Manually, against the running server:**

```bash
uvicorn server.app:app --reload
# then POST any example_query as `message` to /chat, e.g.:
curl -sN -X POST localhost:8000/chat -F "message=How many vendors do we have in total?"
```

**Programmatically**, e.g. to drive a batch of turns through
`harness.agent.run()` in a small script of your own:

```python
import asyncio, json
from harness.agent import run

dataset = json.load(open("eval/local/dataset.json"))
for q in dataset["single_command_queries"]:
    for query in q["example_queries"]:
        answer = asyncio.run(run(query))
        print(q["command"], "->", answer[:120])
```

Every entry in `dataset.json` is read-only by construction (see "Write
operations were removed" above), so running it this way — or via
`eval/local/runner.py` — never triggers the Present-Plan-&-Confirm gate
(Orchestration Protocol step 4). To exercise that gate, use
`python -m eval run --target local --gate` (pause-only) or ask a
write-shaped question by hand (full confirm→execute).

## Observed limitation, found by spot-checking this dataset

Running `single_command_queries["tprm-assessees-count"]`'s example query
("How many vendors do we have in total?") against the real live server (a
real `ANTHROPIC_API_KEY` was available at the time) surfaced a real
behavior gap, caught by looking at `harness.sandbox`'s `cli_call_*` logs
(see README "Proper logging"), not assumed:

**Before a fix**, the model guessed four plausible-but-wrong CLI
invocations in a row (`cybersierra list vendors`, `get vendors`, `vendors`,
`vendors list`) — all four fail, since `cybersierra`'s actual command
shape is `module resource action`, not free-text — read `SKILL.md` once,
guessed two more wrong forms anyway, and gave up, telling the user it
couldn't find the information. This is the same failure mode the sibling
Claude SDK POC documented ("the model sometimes explores before running
the skill's one command"), just worse here because there are 37 real
commands to guess wrong instead of one.

**Fix:** `harness/agent.py`'s `SYSTEM_PROMPT_APPENDIX` gained one more
instruction — read a matching skill's full instructions before running any
command against the CLI it describes, and if that skill says how to
discover the CLI's real command surface, run that discovery step before
guessing. Domain-agnostic wording (no mention of `cybersierra` or
`manifest` — the harness still isn't supposed to know that), since the
*content* of what to discover already lives in `skills/cyber-sierra/
SKILL.md` itself; the fix only pushes the model to actually follow that
content promptly.

**After the fix**, the same query: one wrong guess
(`cybersierra vendors list`), then immediately `cybersierra manifest
--raw` (the discovery step), one near-miss (`tprm assessee count` —
singular; the CLI's own error message even suggested the fix: "Did you
mean assessees?"), then the correct `cybersierra tprm assessees count`,
returning a real number — **"We have a total of 29 vendors."** — in 6
model turns instead of 9, with the actual right answer instead of a
give-up message. Not a perfect zero-guesses run, but a real, verified
improvement, not just a plausible-sounding prompt tweak — see the commit
history / `harness/agent.py`'s `SYSTEM_PROMPT_APPENDIX` comment for the
exact before/after log lines this was checked against.

This is exactly the kind of thing this dataset is for: a single-command
query whose *underlying capability* obviously exists (there's a
`tprm assessees count` command right there in the manifest) turned out to
be a real test of whether the harness actually gets the model to it, not
just whether the capability theoretically exists.

## Second observed limitation, found by running the dataset at scale

Running `eval/local/runner.py` against `health-status-get` and
`notifications-feed-list`/`notifications-feed-unread-count` produced
answers like *"It seems that I can't directly check the status of the
Cyber Sierra backend"* and *"I don't have access to check notifications"*
— the model declining, not attempting the CLI at all correctly. Checking
`harness.sandbox`'s logs for those calls shows **the skill was never
loaded** (no `skill_loaded` line at all) before the model tried a few
guessed, non-`cybersierra` commands (`cybersierra status`,
`cybersierra health-check`, a bare `get-notifications --since=...` —
correctly denied by the sandbox, since it isn't even prefixed with an
allowed binary) and gave up.

**Why:** `skills/cyber-sierra/SKILL.md`'s own frontmatter `description` —
the text `SkillsMiddleware` shows the model to decide whether to read the
full skill — lists trigger words for *"audit, assessment, compliance,
evidence, vendor risk, upload file, run audit, generate report, login"*.
It does not mention system health or notifications anywhere, even though
`cybersierra health status get` and the `notifications` module are real,
current manifest commands (added by a CLI update after this skill file was
originally written — see `generated_from.note` above). A generic query
like "is the backend up" has nothing in that description to match against,
so the model never reads the skill and never learns cybersierra can answer
it at all.

**Left as-is, not fixed**, unlike the subcommand-guessing issue above: the
fix there was a harness-level system-prompt addition that didn't touch the
ported skill content. Fixing *this* would mean editing
`skills/cyber-sierra/SKILL.md`'s own frontmatter `description` — which
this whole port has treated as primary source, copied verbatim, on
purpose (see README "Porting the real pipeline"). This is real, useful
signal about the real skill file rather than something to silently patch
around: the skill's own author-written trigger words undersell the CLI's
actual current scope. If you want this fixed, say so explicitly — it's a
one-line frontmatter edit, but it's a genuine, disclosed exception to
"don't touch the ported skill files," not a harness bug.

## Scored evaluation harness

Beyond `eval/local/runner.py` (captures raw question/answer/tool-call
data), this dataset also has a scoring layer that turns that capture into
precision/recall/F1 against each entry's `expected`/`expected_commands`.
It exists in two independent forms that should agree on a given item's
score:

1. **Local scoring** (`eval/local/score.py` + `scoring.py`) — reads an
   existing results capture (`eval/results/results.json` from
   `eval/local/runner.py`) and scores it entirely offline. No server, no
   Netra, no network access needed. This is the fast, ground-truth path: it
   reads the same SSE-derived `tool_calls` a Netra evaluator would see via
   `trace.tools`, just without the cross-process trace hop.
2. **Netra `tool_accuracy` eval** (`eval/netra/setup_dataset.py` +
   `eval/netra/run.py`) — the same underlying comparison, but scored
   server-side by a Netra evaluator reading `trace.tools` off a real trace,
   so results show up in the Netra dashboard alongside cost/latency/other
   evaluators.

### What "actual invoked commands" means

The real invoked-command signal comes from **two** places, both confirmed
live against a real, authenticated run — not just `run_execution_plan`:

1. `run_execution_plan` tool calls' `args["plan_json"]` (an `ExecutionPlan`,
   see `skills/cyber-sierra/_internal/planner/references/
   execution-plan-schema.md`), specifically each `steps[].command`.
2. Plain `execute` tool calls whose shell command directly runs
   `cybersierra <module> <resource> <action>`. This turned out to matter a
   lot in practice: once auth actually worked end-to-end, real successful
   answers were observed running the CLI command **directly via `execute`,
   never touching `run_execution_plan` at all** — scoring that only trusted
   the latter scored every one of those correct answers as a total miss.
   `_CYBERSIERRA_COMMAND_PATTERN` in `eval/local/scoring.py` extracts these
   without an explicit denylist: discovery/diagnostic calls (`cybersierra
   manifest`, `--help`, `auth whoami`) never have three plain words in a
   row right after `cybersierra`, so they don't match and stay excluded,
   same as before.

`scoring.extract_actual_commands` checks both.

Composite queries with `matchType: "partial"` semantics (order-insensitive
set comparison) can still score a partial precision/recall if the model's
plan invokes some but not all expected commands, or invokes an extra one.
Adversarial queries (empty `expected_commands`) score via a separate
`correct_rejection` boolean (true only when zero commands were invoked)
rather than folding "correctly did nothing" into precision/recall, which
would otherwise reward or penalize it in a way that doesn't mean the same
thing as a normal miss.

### Running the local scorer

```bash
uvicorn server.app:app &
python -m eval.local.runner --service-auth $DEEPAGENT_SERVICE_AUTH
python -m eval.local.score     # scores eval/results/results.json by default
```

Or, in one step: `python -m eval run --target local`.

Writes `eval/results/local_score_summary.json` (gitignored) and prints a
table: per-category and overall micro/macro precision/recall/F1, the
adversarial correct-rejection rate, p50/p95 latency, and (for the two
`auth_scenarios` entries) a leak-check report. Errored turns (server/HTTP
failures) are excluded from scoring, not counted as wrong answers.

### Running the Netra eval (direct-service path)

```bash
# One-time (re-runnable) — creates a 3-item smoke dataset in Netra:
python -m eval.netra.setup_dataset
# ... then, once you're happy with the smoke results:
python -m eval.netra.setup_dataset --full     # scale to all 51 items

python -m eval.netra.run
```

Or, in one step (after the one-time setup above): `python -m eval run --target netra`.

Requires only `NETRA_API_KEY`/`NETRA_OTLP_ENDPOINT`/`NETRA_TRACING` (see
`.env.example`) — no eval tenant, no `MORPHEUS_BACKEND_URL`. See
`eval/netra/EVALUATOR_SETUP.md` for the one-time manual dashboard step
(creating the Tool Correctness and Answer Relevance evaluators) needed
before real scores show up.

## Keeping this current

The CLI changes — this dataset was captured mid-session after a
`cybersierra self-update` had already added a `notifications` module and
three new `tprm` resources that didn't exist when this repo's sandbox
allowlist work started. Run `python -m eval.verify_datasets` any time you
suspect drift (after a CLI update, or periodically) — it re-pulls the live
manifest and flags any entry, in either `eval/local/dataset.json`,
`eval/netra/dataset.json`, or `eval/local/gate_scenarios.json`, whose
`command` string no longer exists or whose `safe` flag changed (in either
direction — see "Write operations were removed" and "Confirm-gate scope"
above for why both datasets and the gate scenarios need opposite checks).
