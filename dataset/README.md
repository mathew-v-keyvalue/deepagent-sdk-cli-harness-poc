# Query dataset

`query_dataset.json` is a set of sample `/chat` queries derived directly
from the real `cybersierra` CLI's manifest — not invented independently.
It exists to answer "what can I actually ask this harness?" with real
examples grounded in real backend capability, and to give the ported
`cyber-sierra` pipeline (Router → Skill Resolver/Planner → Executor →
Reflection) something realistic to be tested against.

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

## Contents

- `single_command_queries` (32) — one entry per **read-only**
  (`safe:true`) manifest command: `module`/`resource`/`action`, the exact
  `command` string, `safe`, `required_params`, and 1–3 `example_queries`.
  The manifest actually has 37 commands (5 are writes) — see "Write
  operations were removed" below.
- `composite_queries` (8) — a natural request plus the `expected_commands`
  sequence a correct Plan should produce (all read-only).
- `adversarial_queries` (5) — requests with no real capability behind them,
  plus why they should be rejected rather than answered with an invented
  command.

## Write operations were removed

The 5 `safe:false` manifest commands (`tprm assessees create`, `tprm
assessments send`/`submit`/`update`, `tprm risk-score-config update-org`)
were removed from this dataset entirely, not just left unexercised.
`dataset/run_dataset.py` calls the real `/chat` endpoint against a real
`cybersierra` backend — a `safe:false` example query in the dataset means
running the dataset **actually creates a vendor, sends/submits/updates a
real assessment, or changes org-level risk scoring** in whatever tenant
the server's `cybersierra` CLI is authenticated against, every single run.
That's a real, repeatable side effect on real data, not a hypothetical —
worth removing outright rather than trusting every future run to remember
not to include them.

`verify/validate_query_dataset.py` enforces this as a hard, ongoing
invariant, not just a one-time cleanup: it fails if any dataset entry is
`safe:false`, and separately fails if a currently-`safe:true` dataset
command's live `safe` flag ever flips to `false` (a read endpoint gaining
a destructive side effect in a future CLI update) — checked in this
session by injecting a fake write-op entry and confirming the script
caught it before restoring the clean dataset.

If you want to specifically exercise the Present-Plan-&-Confirm gate
(Orchestration Protocol step 4 — the model presenting a plan and waiting
for confirmation before running a write), do that by hand against the
running server with a prompt like "Add a new vendor called Acme Corp,"
rather than through this dataset or `run_dataset.py`.

## Running the whole dataset against a real server

```bash
uvicorn server.app:app &            # start the server yourself first
python dataset/run_dataset.py       # defaults: localhost:8000, one example
                                     # query per entry, all three categories
```

Writes `dataset/results.json` (gitignored — it's a run's output, not the
dataset itself, and may contain real data from your cybersierra tenant):
one entry per query with the model's full answer, every tool it called,
token usage, timing, and the `expected` command(s)/behavior from
`query_dataset.json` alongside it for comparison. `--resume` skips queries
already answered in an existing output file; `--all-examples` runs every
`example_queries[]` variant instead of just the first; `--categories
single,composite,adversarial` and `--limit N` narrow a run down. See
`python dataset/run_dataset.py --help` for the rest.

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

dataset = json.load(open("dataset/query_dataset.json"))
for q in dataset["single_command_queries"]:
    for query in q["example_queries"]:
        answer = asyncio.run(run(query))
        print(q["command"], "->", answer[:120])
```

Every entry in this dataset is read-only by construction (see "Write
operations were removed" above), so running it this way — or via
`dataset/run_dataset.py` — never triggers the Present-Plan-&-Confirm gate
(Orchestration Protocol step 4). To exercise that gate, ask a write-shaped
question by hand instead (see that section for an example).

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

Running `dataset/run_dataset.py` against `health-status-get` and
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

## Keeping this current

The CLI changes — this dataset was captured mid-session after a
`cybersierra self-update` had already added a `notifications` module and
three new `tprm` resources that didn't exist when this repo's sandbox
allowlist work started. Run `verify/validate_query_dataset.py` any time
you suspect drift (after a CLI update, or periodically) — it re-pulls the
live manifest and flags any dataset entry whose `command` string no longer
exists or whose `safe` flag changed.
