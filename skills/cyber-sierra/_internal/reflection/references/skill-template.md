# Generated Skill Template

Every learned skill must follow this exact structure. The Reflection component fills in the placeholders from the execution log.

---

## SKILL.md Format

```markdown
---
name: {{SKILL_NAME}}
description: {{SKILL_DESCRIPTION}}
---

# Purpose

{{1-2 sentences: what data this skill fetches and why}}

# When To Use

* "{{example request 1}}"
* "{{example request 2}}"
* "{{example request 3}}"

# Inputs

{{for each input:}}
* {{paramName}} ({{type}}): {{description}} — **required** | optional

{{or if no inputs:}}
No inputs required. All data is fetched for the current tenant.

# Workflow

{{for each step from the execution log:}}

## Step {{N}}: {{human-readable title}}

**Command**: `{{module resource action}}`
**Arguments**:
{{for each argument:}}
- `{{param}}`: `{{value or reference}}`
{{end}}
**Reason**: {{why this step is necessary}}

{{end}}

# Constraints

* Requires authentication — `cybersierra auth whoami` must exit 0
{{additional constraints from reflection improvements}}

# Failure Modes

* Exit 1: API error — check error message in stderr
* Exit 2: Auth expired — re-authenticate with `cybersierra auth login-browser`
* Exit 3: Resource not found — verify IDs and parameters
* Exit 4: Forbidden — verify user permissions

# Examples

* "{{example request 1}}"
* "{{example request 2}}"
* "{{example request 3}}"
```

---

## metadata.json Format

```json
{
  "name": "{{SKILL_NAME}}",
  "description": "{{SKILL_DESCRIPTION}}",
  "source": "planner",
  "learnedFrom": {
    "request": "{{original user request}}",
    "planId": "{{plan ID}}",
    "timestamp": "{{ISO 8601}}"
  },
  "inputs": {
    "{{paramName}}": {
      "type": "{{string|number|boolean|filepath|object|array}}",
      "description": "{{description}}",
      "required": true
    }
  },
  "stepCount": 0,
  "confidence": 0.00
}
```

---

## Naming Rules

- kebab-case, 3–6 words
- Derived from the user's goal
- Must be unique within `_generated/`
- No timestamps, no random suffixes

## Frontmatter Rules

- `name` must match the directory name in `_generated/`
- `description` must be a single line summarizing the data-fetching purpose

## Content Rules

- Workflow steps must reflect the actual execution sequence
- Arguments use `{{inputs.*}}` and `{{step[N].*}}` for generalized references
- Literal constants (pagination defaults, sort orders) remain as-is
- No analysis or reasoning steps
- No conditional logic
