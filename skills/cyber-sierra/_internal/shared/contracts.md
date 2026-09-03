# Component Contracts

Each component in the pipeline has a fixed contract: expected incoming status, required preconditions, owned outputs, and outgoing status.

## Router

| Property    | Value                                                  |
| ----------- | ------------------------------------------------------ |
| Incoming    | `RECEIVED`                                             |
| Outgoing    | `ROUTED`                                               |
| Precondition| `request` is non-empty                                 |
| Outputs     | `goal`, `modules`, `matchedSkill`                      |
| Fails when  | Request cannot be parsed                               |

## Skill Resolver

| Property    | Value                                                  |
| ----------- | ------------------------------------------------------ |
| Incoming    | `ROUTED`                                               |
| Outgoing    | `PLANNED`                                              |
| Precondition| `matchedSkill` is non-null, confidence > 0.8           |
| Outputs     | `plan` (ExecutionPlan with `source: "skill"`)          |
| Fails when  | Skill commands missing from manifest; skill unparseable|

## Planner

| Property    | Value                                                  |
| ----------- | ------------------------------------------------------ |
| Incoming    | `ROUTED`                                               |
| Outgoing    | `PLANNED`                                              |
| Precondition| `matchedSkill` is null, `modules` is non-empty         |
| Outputs     | `plan` (ExecutionPlan with `source: "planner"`)        |
| Fails when  | Manifest cannot satisfy the request                    |

## Executor (deterministic runtime)

| Property    | Value                                                  |
| ----------- | ------------------------------------------------------ |
| Incoming    | `PLANNED`                                              |
| Outgoing    | `EXECUTED`                                             |
| Precondition| `plan` is non-null with at least one step              |
| Outputs     | `executionLog`, `result`                               |
| Fails when  | Auth failure; first non-zero exit code                 |

## Reflection + Learning

| Property    | Value                                                  |
| ----------- | ------------------------------------------------------ |
| Incoming    | `EXECUTED`                                             |
| Outgoing    | `COMPLETE`                                             |
| Precondition| `executionLog` is non-empty, `result` is non-null      |
| Outputs     | `reflection`, optionally `generatedSkill`              |
| Fails when  | Cannot evaluate execution                              |

## Validation Protocol

Every component must:

1. Check `state.status` matches its incoming status. If not, set `error` and return.
2. Check all preconditions. If any fail, set `error` and return.
3. Execute its logic.
4. Set all owned output fields.
5. Advance `status` to its outgoing value.
6. Return state.

The orchestrator validates after each component:
- `status` advanced correctly
- `error` is not set
- Owned output fields are populated

## Exit Codes (CLI)

| Code | Meaning     | Executor behavior                |
| ---- | ----------- | -------------------------------- |
| 0    | Success     | Continue                         |
| 1    | API error   | Halt, surface error              |
| 2    | Auth error  | Halt, prompt re-authentication   |
| 3    | Not found   | Halt, report missing resource    |
| 4    | Forbidden   | Halt, report permission failure  |
