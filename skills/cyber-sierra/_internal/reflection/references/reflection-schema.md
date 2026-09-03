# Reflection Schema

Output produced by the Reflection + Learning component after analyzing execution.

## ReflectionOut

```typescript
ReflectionOut {
  success: boolean          // did the workflow achieve its goal?
  reason: string            // specific explanation
  improvements: string[]    // actionable suggestions for future runs
  shouldPersist: boolean    // should this become a reusable skill?
  confidence: number        // 0.0–1.0
}
```

## Persistence Decision

`shouldPersist` is true only when ALL conditions hold:

1. `result.success === true` — every step exited 0
2. `plan.source === "planner"` — existing skills are not re-learned
3. `plan.steps.length >= 2` — single-step workflows are not worth persisting
4. Workflow is generalizable — arguments use `{{inputs.*}}` references
5. `confidence >= 0.7`

If any condition fails, `shouldPersist = false`.

## Confidence Scoring

| Factor           | Weight | Score                                             |
| ---------------- | ------ | ------------------------------------------------- |
| All steps passed | 0.30   | 1.0 if success, 0.0 otherwise                    |
| Multi-step       | 0.15   | 1.0 if >= 2 steps, 0.5 if 1                      |
| Novelty          | 0.20   | 1.0 if source = planner, 0.0 if skill            |
| Generalizability | 0.20   | Proportion of args using {{inputs.*}} references  |
| Clean execution  | 0.15   | 1.0 if no improvements, scaled down with findings |

## Improvement Categories

When analyzing the execution log, look for:

| Category           | Signal                                                   |
| ------------------ | -------------------------------------------------------- |
| Redundant steps    | A step returned data already available from a prior step |
| Unused output      | A step's output was not consumed by any subsequent step  |
| Slow steps         | A step took disproportionately long                      |
| Over-fetching      | A step fetched far more data than needed                 |
| Missing data       | The user needed data that no step fetched                |

Record each as a concise, actionable string.

## Generated Skill Metadata

When `shouldPersist = true`, the learning phase also produces:

```typescript
SkillMetadata {
  name: string              // kebab-case
  description: string
  source: "planner"
  learnedFrom: {
    request: string         // original user request
    planId: string
    timestamp: string       // ISO 8601
  }
  inputs: {
    [param: string]: {
      type: string
      description: string
      required: boolean
    }
  }
  stepCount: number
  confidence: number
}
```

This is written to `_generated/<name>/metadata.json`.
