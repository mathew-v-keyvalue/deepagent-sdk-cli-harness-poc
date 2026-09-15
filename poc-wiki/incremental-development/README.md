# Incremental development log

One entry per meaningful change made while rebuilding execution modes on
`v1/agent-modes-base` (and onward): what changed, why, and what it affects
downstream. Distinct from `poc-wiki/execution-modes/`, which is the design
record (decisions, open questions) — this folder is the build-order log of
what actually landed, change by change, so anyone picking this up later can
see the path taken without replaying the whole session history.

Empty for now — first entry lands with the first real change.

Suggested shape per entry (one file per change, e.g.
`0001-<short-slug>.md`), not yet enforced:

- **What changed** — the concrete diff, in plain terms.
- **Why** — the decision or requirement behind it.
- **Impact** — what this touches or unblocks (files, downstream work,
  anything that now behaves differently).
