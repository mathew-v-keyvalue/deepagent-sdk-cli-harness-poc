# How Claude Code and Cursor actually build this, at an infra level

`mode-design.md` covers the UX shape (Ask / Agent → Plan / Agent → Auto).
This doc is one level down: what's the actual mechanism each platform
runs on, and where does that map onto pieces we already have (or don't)
in this harness.

## Claude Code's architecture

Three layers, each one able to override the layer below it:

1. **Permission modes** — session-level state (`default`, `plan`,
   `acceptEdits`, `bypassPermissions`), not per-message. Cycled with
   Shift+Tab or set via config; it's one value that governs every tool
   call until it's changed again. This maps directly onto our
   `SessionEntry.mode` design — mode as session state, not a per-request
   flag, is exactly this pattern.
2. **A permission engine evaluated per tool call** — static allow/deny/ask
   rules (`.claude/settings.json`) plus a `canUseTool` callback for
   anything the static rules don't resolve. In **plan mode** specifically,
   mutating tools are never auto-approved regardless of any allow rule —
   they either don't fire or get denied/prompted every time, and the only
   way out is a dedicated tool (`ExitPlanMode`) whose call surfaces the
   plan to the user; approving it is what actually flips the session's
   mode away from `plan`. This is the exact mechanism behind our
   "Plan never executes; you switch to Auto to run it" design — it's not
   a novel idea, it's copying Claude Code's own plan-mode exit pattern
   closely.
3. **Hooks (`PreToolUse`/`PostToolUse`)** — shell commands that intercept
   a tool call *before* the permission engine even runs. A hook's `deny`
   holds even under `bypassPermissions`; a hook's `allow` still can't
   override a static settings-level deny. This is the "belt and braces,
   independent enforcement layers" idea — and it's structurally identical
   to what we already have: `ShellSandboxMiddleware` (layer-2, tool-call
   interception) plus `AllowlistedShellBackend` (layer-1, right before
   the real subprocess call) are our version of exactly this pattern,
   already built, already redundant on purpose.

One thing Claude Code has that we don't have an equivalent of yet:
**`acceptEdits` mode is the closest existing analog to our proposed
Agent → Auto**, but it's a bare toggle — flip into it and edits apply
immediately, no forced first approval. Our one-time-unlock-on-first-write
design is a step *more* cautious than Claude Code's own acceptEdits: we
require one proven approval before unlocking, not just a mode toggle.
Worth knowing this is a deliberate divergence, not an oversight.

## Cursor's architecture

Cursor publishes more about this than most closed-source tools do:

1. **OS-level process sandboxing** — Seatbelt on macOS, Landlock LSM +
   seccomp on Linux (the same primitives Claude Code's CLI itself uses,
   per Cursor's own docs) — a workspace-scoped policy generated at
   runtime that can read/write the workspace and `/tmp`, read the wider
   filesystem, but can't write outside the workspace or hit the network
   without explicit approval. This is a layer *underneath* any
   application-level allow/deny logic — the sandbox holds even if
   Cursor's own permission code has a bug.
2. **The Shadow Workspace** — a second, invisible editor window loaded on
   the same project with the same unsaved changes, where the agent's
   edits get validated (compiled/linted) before the user ever sees them.
   This solves a different problem than ours (code correctness, not
   command safety) — not something this harness needs to replicate, but
   worth naming so it's clear it's not the same mechanism as the
   allowlist below.
3. **"Run Modes" / command allowlist** — a configurable prefix/glob
   allowlist ("commandBase") of what the agent can run without prompting.
   Cursor's own docs are explicit that this is a **"best-effort
   guardrail, not a hard security boundary"** — and in practice it has
   documented gaps (allowlist silently ignored when sandbox mode is on;
   certain shell builtins bypassing the allowlist pre-2.3). This is the
   direct analog of `harness/sandbox.py`'s `ALLOWED_COMMAND_PREFIXES`,
   including sharing the same class of weakness: prefix/glob matching,
   not real shell-grammar parsing.

## What this means for us

**The gap that stands out**: both platforms enforce at two levels — an
application-level permission/allowlist layer *and* an OS-level process
sandbox underneath it. This harness only has the application-level layer
(`sandbox.py`'s prefix allowlist + the proposed mode gating). There's no
OS-level containment if the allowlist logic itself has a bug — which is
exactly the class of gap `open-questions.md` already flags (prefix
matching, not shell-grammar-aware). Both Cursor and Claude Code treat
their allowlist as one guardrail among several, not the only one; right
now, ours is the only one. Not something to solve as part of the modes
work itself, but worth naming as the next real hardening step after
modes ship — e.g. running the sandboxed backend inside its own container/
restricted user rather than trusting prefix matching alone.

**The part we're already matching well**: session-level mode state,
independent redundant enforcement layers, and "plan mode exits rather
than approves-inline" are all patterns we're deliberately mirroring from
Claude Code's own design, not inventing from scratch.

## Sources

- [Iterating with shadow workspaces — Cursor](https://cursor.com/blog/shadow-workspace)
- [Cursor AI Agent Sandboxing Explained](https://www.adwaitx.com/cursor-ai-agent-sandboxing-explained/)
- [Agent Security — Cursor Docs](https://cursor.com/docs/agent/security)
- [The Denylist Delusion: Cursor's Auto-Run — Backslash](https://www.backslash.security/blog/cursor-ai-security-flaw-autorun-denylist)
- [Configure permissions — Claude Agent SDK Docs](https://platform.claude.com/docs/en/agent-sdk/permissions)
- [Claude Code Hooks Complete Guide](https://hidekazu-konishi.com/entry/claude_code_hooks_complete_guide.html)
- [Claude Code Permission Modes and Rule Syntax](https://thepromptshelf.dev/blog/claude-code-permission-modes-complete-guide-2026/)
