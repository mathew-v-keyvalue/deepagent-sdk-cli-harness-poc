# Planning Rules

These rules govern how the Planner generates a Canonical Execution Plan from the CLI manifest.

## Input

The Planner receives:
- `state.request` — the user's original request
- Manifest commands — filtered to the modules identified by the Router

## Process

### 1. Decompose the Request

Split the request into sub-goals:

- **Data sub-goals** — fetching, listing, retrieving. These become plan steps.
- **Analysis sub-goals** — summarizing, comparing, recommending. These are the agent's responsibility post-execution and do NOT become plan steps.

Extract only inputs the user explicitly provided. Do not invent values. If an input is needed but not provided, use `{{inputs.paramName}}` and the Executor will collect it.

### 2. Map Sub-Goals to Commands

For each data sub-goal, find the manifest command whose `description`, `module`, `resource`, and `action` match. If no command matches a data sub-goal, reject the entire request.

### 3. Order by Dependency

- Steps with no dependencies come first.
- Steps referencing `{{step[N].*}}` come after step N.
- No circular dependencies.
- Plans are strictly linear — no branching or conditionals.

### 4. Assemble the Plan

Follow the schema in `references/execution-plan-schema.md`. Every step must have a non-empty `reason`.

### 5. Validate

Run all checks from the validation checklist in `references/execution-plan-schema.md`. If any check fails, reject.

## Hard Rules

1. **No hallucinated commands.** If it is not in the manifest, it does not exist.
2. **No analysis steps.** Skills fetch data. The agent reasons over data.
3. **No partial plans.** If any sub-goal cannot be mapped, reject entirely.
4. **No execution.** The Planner composes plans — it never runs them.
5. **No persistence.** The Planner does not write to `_generated/`.
6. **No branching.** Plans are linear sequences.
7. **Every step needs a reason.** No step may have an empty `reason` field.
8. **No guessing inputs.** Unresolved user values become `{{inputs.*}}` references.
9. **No hardcoded CLI knowledge.** All commands come from the manifest supplied by the Router.

## Rejection Criteria

Reject if any of these are true:
- A data sub-goal has no matching manifest command
- The request requires conditional or branching logic
- The request requires mid-workflow human input that cannot be pre-specified
- Data flow between steps has unresolvable gaps
- The assembled plan fails validation
