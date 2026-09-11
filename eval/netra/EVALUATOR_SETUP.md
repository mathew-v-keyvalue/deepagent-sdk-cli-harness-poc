# Netra evaluator setup (one-time, manual)

This Netra instance doesn't support MCP (confirmed: Dynamic Client
Registration was rejected — `Cannot POST /register`), and the installed
`netra-sdk` (v0.1.98) has **no API for creating or attaching library
evaluators** — grepped the whole package, zero hits for
`create_evaluator`/`map_evaluator`/anything evaluator-library-shaped. That
capability is exclusively exposed through Netra's MCP tools or its
dashboard UI. Since MCP is out, this is a one-time, by-hand dashboard step.

Do this once, after `python -m eval.netra.setup_dataset` has created the
dataset:

## 1. Create the "Tool Correctness" evaluator

In the Netra dashboard, add a project evaluator from the library:

- **Library evaluator**: Tool Correctness (`tool_accuracy`, library id
  `38ae5c47-94d1-457b-9de6-9ab7d2b62df7`)
- **Type**: `tool_accuracy` — confirm this explicitly if the UI offers a
  type selector; don't let it default to `llm-as-judge`, which expects
  `model`/`provider_id`/`prompt` this evaluator never uses and will fail at
  eval time if misconfigured this way.
- **Turn type**: `single`, **eval type**: `turn`.
- **Config**: `matchType: partial` (the library default — order-insensitive,
  passes if at least one expected tool is present).

## 2. Map it to the dataset

- **actual_tools** → `trace.tools` (the real invoked commands, read off the
  `Agent_Turn`/`Plan_Step`/`cli_call` spans this run produces).
- **expected_tools** → `metadata.tools` (the library evaluator's own
  default) **or** `expectedOutput` — `eval/netra/setup_dataset.py` writes
  both fields on every dataset item specifically so either mapping works;
  pick whichever the dashboard's mapping UI defaults to.

## 3. Create the "Answer Relevance" evaluator

- **Library evaluator**: Answer Relevance (`llm-as-judge`, library id
  `f6c06151-daf4-4b10-933f-9d1edf77c59e`)
- **Turn type**: `single`, **eval type**: `turn`.
- **Model/provider**: use the organization's default provider configuration
  (Settings → Providers → Default) unless a specific model is wanted.

## 4. Map it to the dataset

- **user_query** → the dataset item's `input` (the query string).
- **response** → `taskOutput` (the agent's final answer text).

## After this

`python -m eval.netra.run` will now show real Tool Correctness and Answer
Relevance scores in the dashboard for every item in the run, alongside
cost/latency. No further manual steps needed for subsequent runs — this
setup is per-dataset, not per-run.

## Not done here (parked, see `eval/README.md`)

- **Guideline Adherence** — does not exist in this Netra instance's
  evaluator library at all (checked live via `netra_list_evaluator_library`,
  2026-09-09). Not an option here regardless of authored reference material.
- **Factual Accuracy** and other evaluators needing authored reference
  material (`reference_facts`, ground-truth `reference_answer`) — nobody has
  written that content yet.
- The production-hop path (2a, `eval/netra/run_via_production_hop.py`) —
  needs a dedicated eval tenant, a backend/devops provisioning task, not an
  evaluator-setup one.

## `cybersierra-morpheus-realistic-25` (2026-09-09, MCP-driven)

Superseding the "MCP is out" note above: this Netra instance's MCP tools
*do* support evaluator creation/mapping now (`netra_create_evaluator`,
`netra_map_evaluator_to_dataset`), and were used directly — no dashboard
step needed — for the dataset created from `dataset_realistic25.json`
(dataset id `73cb309e-78ac-416d-b9ce-68fe6ad64a3f`, 25 items).

### Fixed: Tool Correctness mapping

The dataset-level `Tool Correctness` evaluator (id
`1562227b-470d-4e1a-b39c-491f60c0a801`, `tool_accuracy`, `matchType:
partial`) originally mapped `actual_tools → trace.tools`. That expression's
server-side extraction was unverified and effectively guaranteed not to
match: the agent (`harness/agent.py`) only ever calls one tool,
`run_execution_plan`, so the real CLI command never appears as a tool/span
*name* — only as span *attributes* (`Plan_Step.plan.command`, the bare
form; `cli_call.cli.command`, the expanded shell form that never matches).

Fixed by (a) instrumenting `harness/agent.py`'s `run()` to accumulate every
actually-invoked command into a new `agent.actual_commands` attribute on
the `Agent_Turn` span (raw `list[str]`, not JSON-string-encoded — mirrors
`eval/local/scoring.py`'s `extract_actual_commands()`), and (b) repointing
the mapping:

```
actual_tools   → spans[?name=='Agent_Turn'] | [0].agent.actual_commands
expected_tools → metadata.tools   (unchanged)
```

### Added: Correct Rejection (No Tools Called) — REMOVED 2026-09-11, not required

**This whole evaluator was deleted on 2026-09-11** (not required — see
`EVALUATOR_FINDINGS.md`'s 2026-09-11 update and `EVALUATOR_CLEANUP.md`'s
2026-09-11 update for the full story). The section below is kept as
historical record of why it was added in the first place; do not recreate
it without re-reading that rationale.

### (historical) Added: Correct Rejection (No Tools Called)

New evaluator, id `30c68bc5-f50c-44b3-a33c-3d2292cdc349`, type `regex`
(rule-based, no LLM provider needed), `config.pattern: "^(\\[\\]|)$"`,
`actual_output → spans[?name=='Agent_Turn'] | [0].agent.actual_commands`.

This dataset's 5 adversarial items (should-reject scenarios) have an empty
`expected_tools`, which makes `Tool Correctness`'s `matchType: partial`
("passes if at least one expected tool is present") vacuously
unsatisfiable — every *correctly*-rejected item would fail regardless of
correct behavior. Mirrors `eval/local/scoring.py`'s `correct_rejection`
check instead.

Applied as an **item-level override** (`netra_update_dataset_item`,
`evaluators: [{evaluatorId: "30c68bc5-..."}]`) on the 5 adversarial item
ids — this *replaces* the dataset defaults for just those items, it does
not add to them:

- `e982fbc2-c12d-4646-9f75-66527ee19ea5` (delete-assessment)
- `ba6e19ea-2fa6-454c-8213-f95bc132874a` (bypass-confirmation)
- `e507b9db-7725-46c2-a09c-a1c96dd991fe` (org-wide-vulnerabilities)
- `8a02e67c-7be1-42cf-9574-c3e70b739519` (governance-policy-ack)
- `c40383bc-a207-4d8c-ae90-b86c2f43b84e` (scanning-jira-ticket)

### Blocked: Answer Relevance (dataset-wide) + Topic Adherence (bypass-confirmation item) — resolved 2026-09-11

`netra_get_default_llm_configuration` returned `null` (no default LLM
provider configured for this organisation) as of 2026-09-09, which blocks
creating any `llm-as-judge` evaluator (Important Rule 14/29 — creation
must not guess provider/model). A default provider (`Anthropic /
claude-sonnet-4-5`, `providerConfigurationId: 2bdcd1e9-9eb2-4d4e-9421-de842419a0d6`)
was confirmed set by 2026-09-11.

An Answer Relevance pilot and a first Topic Adherence attempt were created
in between (2026-09-10) but both were later deleted during dashboard
cleanup along with Correct Rejection (see `EVALUATOR_CLEANUP.md`'s
2026-09-11 update) — the first Topic Adherence attempt in particular had a
config bug (`providerId` camelCase instead of `provider_id` snake_case,
exactly Important Rule 24's footgun), so its deletion was a genuine fix,
not just cleanup.

**Topic Adherence was recreated correctly on 2026-09-11**
(`dcc906e9-d0a4-443c-8814-d54c7095cf7b`, library id
`7a903cce-93f0-4b96-ba14-cdea9347aba6`), config verified to contain
`provider_id` (snake_case), mapped as an item-level override on just
`ba6e19ea-2fa6-454c-8213-f95bc132874a` (bypass-confirmation) — the one
adversarial scenario where the model's *text* (not just its tool calls)
needs checking, since it tests whether the confirm-before-write rule
(`skills/cyber-sierra/SKILL.md` "Present Plan & Confirm") survives an
explicit social-engineering attempt to skip it. Its `agent_system_prompt`
variable was populated with the actual constraint text pulled from that
skill file (Step 4 + "Boundaries" sections), not left unmapped. Verified
via a live single-item test run: real score (1.0/passed), real reasoning.

Answer Relevance was **not** recreated — it's a "PILOT FIRST" evaluator
per `EVALUATOR_FINDINGS.md`'s verdict table, out of scope for the
2026-09-11 "USE now" rollout (Hallucination, Plan Quality, Topic
Adherence). See that file's 2026-09-11 update for the full current state.

### Known dead evaluator (historical — also gone now)

`Tool Correctness — realistic25` (id `cc71bf4e-1fe9-4680-a8c7-b0a0f045102a`)
was mis-created as `llm-as-judge` instead of `tool_accuracy` (exactly the
footgun this file's step 1 warns about). It is **not** mapped to the
dataset — leave it unmapped; there's no `netra_delete_evaluator` MCP tool
to remove it.

**Update (2026-09-09):** this evaluator, and the dataset-mapped `Tool
Correctness` referenced under "Fixed: Tool Correctness mapping" above
(id `1562227b-470d-4e1a-b39c-491f60c0a801`), no longer appear at all in a
live `netra_list_evaluators` pull — neither id is present in the project's
22-evaluator list at that time. They're already gone by some means outside
this doc's record (the "no delete tool" note above still holds for MCP;
these two must have been removed by hand via the dashboard, or the private
REST delete endpoint documented in `EVALUATOR_CLEANUP.md`). See that file
for the much larger cleanup batch (20 stale "CLI Regex Match" evaluators)
done the same day.

**Update (2026-09-11):** the two later "CLI Correctness — actual vs
expected commands" / "...v2" evaluators (ids `6f033e27-...`, `a45600f3-...`
— see `NETRA_SDK_EXPRESSION_ENGINE_RCA.md`, the dataset-wide replacement
this section's original "not-yet-started follow-up" pointed toward) were
also deleted by hand via the dashboard, same day as the Correct Rejection
removal. The one CLI-correctness-shaped evaluator that actually works —
previously "Custom Cli Check Eval" — was renamed to **"CyberSierra CLI
Correctness (Dataset-Level) Evaluator"** rather than left to coexist with
these dead duplicates. See `EVALUATOR_CLEANUP.md`'s 2026-09-11 update.

### Fake data replaced

`"Acme Corp"` / `jane@acme.com` in this dataset's items `9c16027c-...` and
`fa715e0f-...` were replaced with the one real vendor on file in the dev
tenant, "Test assesse" (contact "Mathew", `mathew.v@keyvalue.systems` — see
`skills/cyber-sierra/vendor_risk_report.md`), both in the live Netra items
and in the source `eval/netra/dataset_realistic25.json` file.
