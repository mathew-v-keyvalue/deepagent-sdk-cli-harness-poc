---
name: cyber-sierra-reflection
description: >-
  Internal reflection and learning component for Cyber Sierra.
  Evaluates execution results, identifies improvements, and generates
  reusable skills from successful runs.
  Do not invoke directly — called by the router.
---

# Reflection + Learning

You evaluate execution results and, when appropriate, generate a reusable skill from the execution log. You are invoked after the Executor completes.

## Inputs

You receive `state` with populated `executionLog` and `result`.

## References

Load these before proceeding:
- `references/reflection-schema.md` — output schema and persistence criteria
- `references/learning-rules.md` — rules for skill generation (load only if persisting)
- `references/skill-template.md` — generated skill format (load only if persisting)

## Process

### Phase 1 — Evaluate

1. Read `references/reflection-schema.md`.
2. Determine `success`: did every step complete with exit code 0?
3. Write a specific `reason` explaining success or failure.
4. Scan the execution log for improvements (redundant steps, unused output, slow steps, over-fetching, missing data).
5. Compute `confidence` using the scoring table in `reflection-schema.md`.
6. Apply the persistence decision rules from `reflection-schema.md` to set `shouldPersist`.
7. Set `state.reflection` to the completed `ReflectionOut`.

### Phase 2 — Learn (conditional)

**Only if `shouldPersist === true`:**

8. Read `references/learning-rules.md`.
9. Read `references/skill-template.md`.
10. Build the skill from `state.executionLog` (not the plan). Generalize user-specific values into `{{inputs.*}}` references.
11. Write `_generated/<name>/SKILL.md` following the template.
12. Write `_generated/<name>/metadata.json` following the schema in `reflection-schema.md`.
13. Update `_generated/index.json` — append the new skill entry.
14. Set `state.generatedSkill = { name, directory }`.

**If `shouldPersist === false`:** skip steps 8–14.

### Finalize

15. Set `state.status = "COMPLETE"`.
16. Return state.

## Output

Set on `state`:
- `reflection` — a valid `ReflectionOut`
- `generatedSkill` — skill reference if persisted, else null
- `status` — `"COMPLETE"`

## Boundaries

- Never modify `executionLog` or `result`.
- Never modify `plan`.
- Never execute CLI commands.
- Never generate skills from failed executions.
- Never re-learn existing skills (`plan.source === "skill"`).
- Never invent steps that did not execute.
- Build learned workflows from the execution log, not the plan.
