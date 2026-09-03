# Workflow State

Shared state object that flows through the pipeline. Each component mutates only its owned fields.

## Schema

```typescript
WorkflowState {
  request: string                    // raw user request
  goal: string                       // parsed intent
  modules: string[]                  // CLI modules relevant to this request
  matchedSkill: null | SkillMatch    // Router output
  plan: null | ExecutionPlan         // Planner or Skill Resolver output
  executionLog: ExecutionLogEntry[]  // Executor output
  result: null | ExecutionResult     // Executor output
  reflection: null | ReflectionOut   // Reflection output
  generatedSkill: null | SkillRef    // Learning output
  status: Status                     // current pipeline position
  error: null | PipelineError        // set on fatal failure
}
```

## Supporting Types

```typescript
SkillMatch {
  name: string
  directory: string      // path to _generated/<name>/
  confidence: number     // 0.0–1.0, must exceed 0.8
}

ExecutionResult {
  success: boolean
  finalOutput: any
  stepsCompleted: number
  stepsFailed: number
}

SkillRef {
  name: string
  directory: string      // path written to _generated/<name>/
}

PipelineError {
  component: string
  message: string
  status: Status         // status when error occurred
}
```

## Status Enum

```
RECEIVED -> ROUTED -> PLANNED -> EXECUTED -> COMPLETE
```

Reflection advances directly from `EXECUTED` to `COMPLETE`. Learning (skill persistence) is an internal step within Reflection, not a separate pipeline stage.

## Ownership

| Component       | Reads                        | Writes                                      |
| --------------- | ---------------------------- | ------------------------------------------- |
| Router          | `request`                    | `goal`, `modules`, `matchedSkill`, `status` |
| Skill Resolver  | `matchedSkill`               | `plan`, `status`                            |
| Planner         | `request`, `modules`         | `plan`, `status`                            |
| Executor        | `plan`                       | `executionLog`, `result`, `status`          |
| Reflection      | `executionLog`, `result`     | `reflection`, `generatedSkill`, `status`    |

Rules:
1. Components must not write fields they do not own.
2. `status` is the only shared-write field.
3. Once a component writes a non-status field, that field is frozen.
4. Any component may set `error` to halt the pipeline.

## Initialization

```json
{
  "request": "<user request>",
  "goal": "",
  "modules": [],
  "matchedSkill": null,
  "plan": null,
  "executionLog": [],
  "result": null,
  "reflection": null,
  "generatedSkill": null,
  "status": "RECEIVED",
  "error": null
}
```

## Transition Rules

1. Forward only. No backward transitions.
2. Each component advances status exactly one step.
3. Only the designated component may perform a given transition.
4. If `error` is set at any point, the pipeline halts.
5. Exactly one of Planner or Skill Resolver runs per workflow.
