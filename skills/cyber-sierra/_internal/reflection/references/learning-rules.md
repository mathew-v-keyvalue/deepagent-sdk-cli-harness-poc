# Learning Rules

These rules govern how the Reflection component generates reusable skills from successful executions.

## Preconditions

The learning phase runs only when `shouldPersist === true`. This requires:
- All steps succeeded
- Plan originated from the Planner (not replaying an existing skill)
- Multi-step workflow (>= 2 steps)
- Confidence >= 0.7

## Source of Truth

Build the learned skill from the **execution log**, not the plan.

| Plan (intent)                        | Execution log (reality)                |
| ------------------------------------ | -------------------------------------- |
| What was supposed to happen          | What actually happened                 |
| May contain untested assumptions     | Verified by successful execution       |
| Arguments may be speculative         | Arguments are resolved and validated   |

The learned skill reflects reality.

## Generalization

Replace user-specific values with reference syntax so the skill is reusable:

1. Compare each resolved argument in `executionLog[].arguments` against the plan's `{{inputs.*}}` references.
2. Where the plan used `{{inputs.param}}`, the log has the concrete value. Restore the `{{inputs.param}}` reference.
3. Where the plan used `{{step[N].path}}`, restore that reference.
4. Keep literal constants (pagination defaults, sort orders, etc.) as-is.

## Naming

- Derive the name from `state.goal`, not from IDs or timestamps.
- Use kebab-case, 3–6 words.
- Check `_generated/index.json` for uniqueness. If a collision exists, append `-2`, `-3`, etc.

## File Structure

Each learned skill gets its own directory:

```
_generated/<name>/
  SKILL.md          # the reusable skill (human-readable, used by Skill Resolver)
  metadata.json     # machine-readable provenance (used for debugging and auditing)
```

**Two stores, two purposes:**
- `index.json` is for **discovery** — the Router reads it to match requests without loading every skill. It contains just enough for scoring: name, description, examples.
- `metadata.json` is for **provenance** — records how and when the skill was learned. It is never read during normal routing.

## Index Update

After writing the skill files, update `_generated/index.json`:

```json
{
  "skills": [
    {
      "name": "<name>",
      "description": "<description>",
      "directory": "<name>",
      "examples": ["<example request 1>", "<example request 2>"],
      "confidence": 0.XX,
      "learnedAt": "<ISO 8601>"
    }
  ]
}
```

Append the new entry to the existing `skills` array. Do not replace it.

## Hard Rules

1. **Never learn from failed executions.** If any step failed, do not persist.
2. **Never re-learn existing skills.** If `plan.source === "skill"`, do not persist.
3. **Never invent steps.** The workflow contains exactly the steps that executed.
4. **Never modify execution history.** The log is read-only.
5. **Build from log, not plan.** The execution log is truth; the plan is context.
6. **Generalize user-specific values.** Replace concrete inputs with `{{inputs.*}}`.
7. **Follow the template.** Use `references/skill-template.md` exactly.
