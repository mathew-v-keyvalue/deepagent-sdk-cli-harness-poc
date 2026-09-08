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
  `Agent_Turn`/`Plan_Step`/`CLI_Call` spans this run produces).
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

- **Guideline Adherence**, **Factual Accuracy**, and other evaluators
  needing authored reference material (`assistant_instructions`,
  `reference_facts`, ground-truth `reference_answer`) — nobody has written
  that content yet.
- The production-hop path (2a, `eval/netra/run_via_production_hop.py`) —
  needs a dedicated eval tenant, a backend/devops provisioning task, not an
  evaluator-setup one.
