# Canonical Execution Plan Schema

This is the intermediate representation shared by the Planner and Skill Resolver. The Executor consumes only this schema. It never knows the plan's origin.

## ExecutionPlan

```typescript
ExecutionPlan {
  planId: string             // kebab-case identifier, e.g. "vendor-risk-fetch-a1b2"
  source: "planner" | "skill"
  sourceName: string         // "planner" or the matched skill name
  steps: ExecutionStep[]
}
```

## ExecutionStep

```typescript
ExecutionStep {
  id: number                 // 1-indexed, sequential
  command: string            // manifest triple: "module resource action"
  arguments: {
    [param: string]:
      string | number | boolean | object
  }
  safe: boolean              // from manifest: true = read-only, false = write (requires user confirmation)
  reason: string             // why this step exists — required, never empty
  expectedOutput: string     // human-readable description of expected return
  dependsOn: number[]        // step IDs this step depends on (empty = independent)
}
```

## Reference Syntax

Arguments may contain references that the Executor resolves at runtime:

| Syntax              | Resolves to                                        |
| ------------------- | -------------------------------------------------- |
| `{{inputs.field}}`  | User-provided value collected before execution     |
| `{{step[N].path}}`  | Value from step N's stdout, navigating JSON path   |

Rules:
- `N` in `{{step[N].*}}` is 1-indexed and must reference a prior step (`N < current id`)
- Forward references are invalid
- Unresolvable references halt execution

## ExecutionLogEntry

Produced by the Executor for each step:

```typescript
ExecutionLogEntry {
  stepId: number
  command: string            // resolved command
  arguments: object          // resolved arguments (no {{}} references)
  stdout: string
  stderr: string
  exitCode: number
  success: boolean           // exitCode === 0
  duration: number           // milliseconds
  timestamp: string          // ISO 8601
}
```

## Validation Checklist

A valid plan satisfies all of:

1. `steps.length >= 1`
2. Every `step.command` exists in the manifest catalog
3. Every `step.id` is unique and sequential starting from 1
4. Every `{{step[N].*}}` has N < current step id
5. Every required manifest param has a value or reference
6. Every `step.reason` is non-empty
7. No step exists solely for analysis or summarization
8. `dependsOn` forms an acyclic graph
