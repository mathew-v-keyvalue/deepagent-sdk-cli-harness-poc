# Netra evaluator findings — what we tried, why each one falls short, what's left to decide

Context: the actual thing we need evaluated is not just "did the agent call
a tool" but **"did the agent invoke the correct `cybersierra` CLI command
with the correct arguments?"** — correctness lives at the CLI-options level,
not the tool-name level. Every built-in Netra library evaluator we tried
falls short of that bar in a different way. This note walks through each
one, why it doesn't work as-is, and what actually does.

## 1. Tool Correctness (`tool_accuracy`)

**What it checks:** which *tools* were invoked, matched against an
expected-tools list.

**Why it's not enough:** in this harness, the tool surface is generic —
`cli_call`, `read`, and so on — not one tool per CLI command. So Tool
Correctness can only ever confirm "`cli_call` ran," never "`cli_call` ran
with the right options." It has no visibility into the tool's *input* —
the actual CLI command and arguments — which is the part that actually
determines whether the agent did the right thing. It resolves at the wrong
level of granularity for what we need to test.

## 2. Regex evaluator (multi-turn / confirmation flow)

**What it checks:** matches a configured regex pattern against a target
field.

**Why it's not enough here:** for the confirm-before-write flow (skill's
"Present Plan & Confirm" gate), the field we'd want to match against is
the agent's final answer — but that's free-form, naturally worded text.
There's no reliable regex to write against it, since a correct confirmation
message can be phrased many different ways.

**Workaround (already confirmed working):** stop evaluation at the *plan*
phase instead of the final worded answer — match the regex against the
structured plan the agent produces before execution, rather than the
free-text confirmation message after. This is the same approach already
validated for Tool Correctness (comparing `agent.actual_commands` off the
`Agent_Turn` span): the plan is structured and matchable, the final answer
text isn't.

## 3. JSON evaluator

**What it checks:** structured input/output comparison.

**Why it's a problem:** this one actually gets the right data — the real
input (CLI command + args) and output — but it only supports strict
equality, no regex/pattern matching. To use it, every dataset item would
need the *exact* CLI command with the *exact* arguments hand-authored as
the expected value. That makes the evaluation extremely tight/brittle: any
harmless variation (arg order, an equivalent flag, formatting) fails the
item even when the agent did the right thing.

## 4. Custom evaluators — confirmed working

Using Netra's custom evaluator path (our own regex/code-based logic
instead of a library evaluator) does work: we can pattern-match the actual
CLI command + arguments instead of requiring exact equality, which gets us
correctness at the right granularity (command + args, not just tool name)
without the brittleness of the JSON evaluator's exact-match requirement.

This is the same mechanism already used for the "Correct Rejection"
evaluator on adversarial items (see `EVALUATOR_SETUP.md`) — a `regex`-type
custom evaluator, no LLM judge needed.

## Net conclusion

None of Netra's off-the-shelf library evaluators validate at the level we
actually need (CLI command + arguments) without either being too shallow
(Tool Correctness only sees the tool name) or too strict (JSON evaluator
requires byte-for-byte match). Custom, pattern-based evaluators are the
only path that's actually confirmed to work for CLI-correctness checking.

## What's left to decide

1. **Standardize on custom (regex/code) evaluators for CLI correctness**,
   rather than Tool Correctness or the JSON evaluator, across the dataset —
   confirm this is acceptable as the primary evaluation mechanism rather
   than a fallback.
2. **Authoring effort/ownership** — custom evaluators need a
   correctness pattern (regex or code) written per command/scenario,
   instead of just listing `expected_tools`. Decide who owns writing and
   maintaining these patterns as the CLI's command surface evolves.
3. **Confirm-gate (write-flow) scoring** — decide whether to standardize on
   "stop at the plan phase, compare the structured plan" for all
   confirm-before-write scenarios, or invest further in evaluating the
   final worded confirmation text too.

---

# Full library survey — every evaluator Netra offers, and its relevance here

Pulled live from `netra_list_evaluator_library` (2026-09-09, 39 evaluators
across 8 categories). The four already covered above (Tool Correctness,
Regex, JSON, and custom) aren't repeated below. Grouped by how usable each
one actually is for this harness today, not just by Netra's own category
labels.

## Live account check (2026-09-09) — what's actually created today

Pulled via `netra_list_evaluators`: **22 evaluators exist in this project,
and all 22 are rule-based (`regex` type)** — the 20 per-command "CLI Regex
Match — ..." evaluators (one per manifest command in the realistic-25
dataset, matching `cybersierra <module> <resource> <action>` against the
real `cli_call` span input) plus "Correct Rejection" and one leftover
diagnostic control. **Zero `llm-as-judge` evaluators have been created
yet.** So every evaluator below this line is a genuinely new spend
decision, not something already running — nothing to rip out, just a
choice about what to turn on.

Also worth noting: both `cybersierra-morpheus-realistic-25` and its
`-noconfirm` variant currently show `evaluators: []` at the **dataset**
level — none of the 20 CLI Regex Match evaluators are wired up as dataset-
or item-level mappings yet either. That wiring is a separate remaining
step from creating them.

## Verdict — use it or not, per Netra library evaluator

Every evaluator below is one Netra ships in its own library (the "Regex
Evaluator" and "Tool Correctness" rows are the generic library forms of
mechanisms already in active use — see the account check above). Graded
on whether it earns its cost/effort given what's already covered for
free, not just on topical relevance:

| Library evaluator | Verdict | Why |
|---|---|---|
| **Hallucination** | **USE** | Only thing that checks the final answer's facts against real CLI output — regex can't do this. Real gap, real value. |
| **Plan Quality** | **USE** | Small fixed scope (5 confirm-gate items), matches our validated plan-phase approach. |
| **Topic Adherence** | **USE** | 1 item only (bypass-confirmation) → trivial cost, catches a real designed-for risk. |
| **Regex Evaluator** (library) | **USE** | Already the mechanism behind our 20 custom command-matchers — this *is* that library evaluator, just instantiated per command. Free. |
| **Faithfulness** | **DON'T USE** | Same signal as Hallucination, inverted scoring. Running both doubles cost for nothing new. |
| **Tool Correctness** | **DON'T USE** | Only sees tool name (`cli_call`), blind to args — superseded by our own command-level regex checks. |
| **JSON Evaluator** | **DON'T USE** | Exact-match only, brittle — superseded by regex. |
| **Bias** / **Toxicity** | **DON'T USE** | Built for public-facing chat risk. Internal compliance tool, no adversarial-content exposure in the dataset — low signal for the LLM cost. |
| **Conciseness** | **DON'T USE** | No correctness value, pure style — not worth paying for. |
| **Context Precision** / **Context Recall** / **Context Relevance** | **DON'T USE** | Built for RAG retrievers. This harness calls a CLI, doesn't retrieve documents — doesn't apply. |
| All 9 **Multimodal** evaluators | **DON'T USE** | No images anywhere in this agent. |
| **SQL Semantic Equivalence** | **DON'T USE** | No SQL generation here. |
| **Answer Correctness** | **DON'T USE (yet)** | Needs a hand-authored `reference_answer` per item — nobody's written that. Authoring cost + LLM cost with nothing built. |
| **Goal Accuracy** | **DON'T USE (yet)** | Same problem — needs authored `expected_outcome` per item. |
| **Factual Accuracy** (both variants) | **DON'T USE (yet)** | Same problem — needs authored `reference`/`reference_facts`. |
| **Information Elicitation** | **DON'T USE (yet)** | No dataset scenario currently tests "should have asked instead of guessed" — nothing to evaluate. |
| **Answer Relevance** | **PILOT FIRST** | Once Hallucination + regex cover correctness, this mostly checks wording quality — real but lower stakes. Try it on a few items before deciding dataset-wide. |
| **Semantic Similarity** | **PILOT FIRST** | Cheaper than a full judge (embeddings), but still needs a reference answer per item — same authoring gap as Answer Correctness, worth testing only if that content ever gets written. |
| **Cost** / **Latency** / **Token Usage** | **DEFER** | Mechanically free (no LLM) — schema only accepts a fixed `exact_value` threshold, no built-in baseline learning from historical runs. Wire up once there are stable, repeated runs to baseline from (see below). |
| **Guideline Adherence** | **DEFER** | Real value (tests the confirm-gate rule across a full session) but needs Netra's multi-turn simulation infra — separate build, not a day-one add. **Note:** recorded as fully absent from this instance's library on 2026-09-09 in `EVALUATOR_SETUP.md`; a fresh pull just now (same day) shows it present (id `64cdb049-...`) — that old note is stale, confirmed live. |
| **Goal Fulfillment** | **DEFER** | Same — needs simulation infra plus a write-capable eval tenant that doesn't exist yet. |
| **Conversation Completeness** / **Conversation Memory** / **Conversational Flow** | **DEFER** | General session-health checks, not correctness signals, and also need simulation infra. Lowest priority of the multi-turn group. |

Net: 4 use now, 2 pilot, rest either don't apply or aren't worth the
spend/effort yet.

### How to actually set Cost/Latency/Token thresholds

Confirmed from the live schema: these three evaluators require a fixed
number (`exact_value`) as the "expected" ceiling — Netra has no built-in
mechanism to learn a normal baseline from historical runs. So a threshold
set today, before the POC is stable, would be arbitrary and would either
false-positive on normal variance or be set so loose it catches nothing.

Recommended sequencing: **don't wire these up as pass/fail gates yet.**
Once the agent's behavior stops changing turn-to-turn (stable prompt,
stable tool surface), run the dataset repeatedly, capture the actual
cost/latency/token numbers Netra already records per trace, take something
like the observed p95 as a baseline, and set the threshold generously
above that (e.g. 1.5–2x) purely to catch a severe regression — not as a
"correct" target. Revisit the number periodically as the agent changes,
the same way any performance regression gate gets tuned. This isn't a
Netra gap specifically — no regression threshold is meaningful before
there's stable data to set it from.

---

## Update (2026-09-11) — Correct Rejection removed, CLI Correctness consolidated, 3 "USE now" evaluators added

Everything in this section reflects the live project state as of today, superseding the
stale "22 evaluators" / "zero llm-as-judge evaluators" account-check snapshot above (that
was already outdated before today's changes too).

**Removed:** `Correct Rejection (No Tools Called)` — not required. Deleted directly via the
Netra dashboard's "My Evaluators" section (no `netra_delete_evaluator` MCP tool exists, same
constraint noted in `EVALUATOR_CLEANUP.md`). Its 5 item-level overrides (on the confirm-gate/
adversarial items) were separately cleared via `netra_update_dataset_item(evaluators=[])` —
deleting the evaluator record alone does not clear item-level override pointers, they're
independent state.

**Consolidated:** both dead "CLI Correctness — actual vs expected commands" / "...v2"
evaluators were deleted (rather than renamed) — the one evaluator that was actually working,
previously called "Custom Cli Check Eval" (a `code`-type evaluator active on the
`-noconfirm` dataset), was renamed to **"CyberSierra CLI Correctness (Dataset-Level)
Evaluator"** instead. One clear, honestly-named evaluator instead of three overlapping ones.

**Added, verified working (real scores, `provider_id` config confirmed present each time):**
- **Topic Adherence** (`dcc906e9-d0a4-443c-8814-d54c7095cf7b`) — item-level override on
  `ba6e19ea-...` (bypass-confirmation) only, with `agent_system_prompt` populated from the
  real constraint text in `skills/cyber-sierra/SKILL.md` (Step 4 "Present Plan & Confirm" +
  "Boundaries" sections), not left unmapped.
- **Plan Quality** (`d2851bf1-a8d7-4e41-8843-956128e70584`) — item-level override on all 5
  confirm-gate items.
- Both confirmed via live single-item test runs with real, non-null scores and reasoning
  (e.g. Plan Quality 0.5/failed with "Plan lacks concrete steps..." — a real judgment, not a
  config error).

**Added, then dropped — `spans`-rooted mapping broken for `llm-as-judge`:** **Hallucination**
(`fd7fd645-3e6e-4479-9c52-f7a82fb36dc4`) was created and mapped (dataset-wide, plus on the 5
confirm-gate items) to check the final answer against real CLI output via a new
`retrieved_context` variable. New harness instrumentation was added to support it (see
below) — `agent.actual_outputs`, verified correct on multiple live traces. But every
`spans`-rooted mapping expression tried for `retrieved_context` resolved to null at eval
time, while the *identical* variable mapped to `taskOutput` instead resolved correctly
(control test, isolated via a cheap no-agent-cost debug harness — see
`NETRA_SDK_EXPRESSION_ENGINE_RCA.md`'s 2026-09-11 addendum for the full isolation, including
ruling out the `bucket: user_provided` tag as the cause). This conclusively points at a
platform-side bug specific to `spans`-rooted variables on `llm-as-judge` evaluators, not this
harness's instrumentation or expression syntax. **Decision: dropped, not shipped** —
Hallucination is deactivated dataset-wide and removed from all item-level overrides. Topic
Adherence and Plan Quality (neither depends on `spans`) shipped as planned. Revisit once
Netra confirms whether this is a known/fixable platform issue.

**Separately, unrelated:** mid-investigation, the org's configured Anthropic provider
(`providerConfigurationId 2bdcd1e9-9eb2-4d4e-9421-de842419a0d6`) ran out of API credits
server-side (`400: Your credit balance is too low`) — this blocks *all* `llm-as-judge`
evaluators (including the now-working Topic Adherence/Plan Quality) until recharged by the
account owner. This key lives in Netra's own dashboard (Settings → Providers), separate from
this repo's `.env` `ANTHROPIC_API_KEY` (which only powers the agent under test, not the
judge) — a real point of confusion worth flagging to whoever manages Netra billing.

**New harness instrumentation (`harness/agent.py`):** a parallel `agent.actual_outputs`
list[str] attribute on the `Agent_Turn` span, mirroring the existing `agent.actual_commands`
attribute — accumulated via a new `on_tool_end` handler (there was none before; only
`on_tool_start` existed). Investigating this surfaced a stale assumption in the codebase's
own comments: they claimed only `run_execution_plan` is reachable ("not reachable today" for
raw `execute` calls) — live traces confirm the opposite is true today. The model calls
`execute` (raw shell) directly, not `run_execution_plan`, so the real fix captures `execute`'s
`ToolMessage.content` (already plain CLI stdout text) directly; the `run_execution_plan`
branch is kept for symmetry/future-proofing via the existing JSON-envelope-parsing
`_extract_plan_outputs()` helper, in case that tool is ever actually invoked.
