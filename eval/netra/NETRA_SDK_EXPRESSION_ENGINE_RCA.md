# RCA: Netra's server-side evaluator expressions don't correctly evaluate `spans[?name=='cli_call']`-style filters

## Impact

A custom `regex`-type evaluator mapped to a dataset via
`netra_map_evaluator_to_dataset`, with `actual_output` sourced from an
expression referencing `spans[?name=='cli_call']` (filtering to a
specific span name among several matches), does not evaluate the filter at
runtime. It silently returns one arbitrary span's raw `input` value
instead of the intended computed result — with **no error, no rejected
mapping, no visible sign anything is wrong** short of manually reading a
resolved `actual_output` and recognizing it doesn't match what the
expression should have produced. This makes any dataset-wide CLI/tool
correctness evaluator built this way unusable, and — worse — silently
wrong rather than cleanly broken.

## What was tested

Netra's evaluator expressions look like JMESPath syntactically (filter
projections, pipes, `contains`/`join`/`to_string`, `&&`/`||`, backtick
literals), and a *validator* endpoint (hit when calling
`netra_map_evaluator_to_dataset`) does perform real JMESPath-like syntax
checking — e.g. it correctly rejected `length(...)` outright ("The
JMESPath expression is invalid") while accepting `join`, `contains`,
`to_string`, `&&`, `==`, `!=` individually. But passing that validator
does not mean the expression is evaluated correctly at runtime, against a
real trace, during actual scoring. Three expression shapes were mapped and
each run against the same real item — a query whose agent run genuinely
invoked 6 real `cli_call` spans, one of them (the last) being the exact
expected command `cybersierra tprm risk-score-config get-org`:

1. **Full nested boolean** (3 `&&`/`||` clauses, comparing `metadata.tools`
   against `join('\n', spans[?name=='cli_call'].input)`) — resolved
   `actual_output` to the raw string `"cybersierra tprm risk-score-config
   get-org"` (the last span's `input`, unwrapped), not `"true"`/`"false"`.
2. **Single `to_string(contains(...))`** (no boolean tree at all, same two
   operands) — identical result: the same raw last-span string.
3. **Bare `spans[?name=='cli_call'].input`**, no wrapping function
   whatsoever — on a *different* real item (3 `cli_call` spans, the agent
   having stopped before the target command), still resolved to one
   specific span's raw `input` (that trace's last span with an `input`
   attribute), not an array or joined multi-line value of all 3 matches.

All three collapse to "one span's raw value," regardless of the presence
or absence of `join`/`contains`/`to_string`/boolean combinators wrapping
it. This rules out "our expression was too complex" — the minimal, bare
projection (case 3) shows the identical symptom. The consistent behavior
across cases strongly suggests the `[?name=='cli_call']` filter condition
itself is not applied at runtime; something in Netra's evaluator falls
back to "the last span with an `input` attribute" regardless of the
`name` filter.

## What is confirmed vs. not

**Confirmed** (directly observed, reproduced 3x): passing the map-time
validator does not guarantee correct runtime evaluation; multi-match
`spans[?name==X].field`-style projections do not behave as documented
JMESPath semantics would predict.

**Not confirmed** (no visibility into Netra's server implementation): the
*exact* internal reason — whether it's the filter clause being dropped,
the projection being collapsed to a single element, or something else
entirely in how the expression is parsed/executed server-side. This
would need a much larger sweep (isolating `.input` projection alone vs.
the filter alone, across more span-count variations) or, more practically,
direct input from Netra's own engineers, to pin down further from outside
their codebase.

## Workaround attempted, then reverted (2026-09-09)

A local-evaluator approach was prototyped: `netra-sdk` exposes exactly this
path (`Netra.evaluation.run_test_suite(..., evaluators=[...])`, subclassing
`netra.evaluation.evaluator.BaseEvaluator`) — the evaluator's `.evaluate()`
runs in-process, in plain Python, with no server-side expression
evaluation involved at all. It required a small `harness/agent.py::run()`
signature change (an optional `actual_commands_out` out-param) plus a
module-level side-channel in `eval/netra/task.py` and a new
`eval/netra/local_evaluators.py`. This was reverted before landing —
touching `harness/agent.py` (shared with the live server, not just eval)
wasn't something to change without more deliberate review — so none of
that code exists in the repo right now. The diagnosis above (the
expression engine bug itself) stands regardless; only the code fix was
rolled back. Re-attempt later with more care around the harness change,
or find a fix that stays entirely inside `eval/netra/`.

The now-proven-unreliable server-side mapping (`CLI Correctness — actual
vs expected commands v3`, evaluator id `e2efce2b-2f84-4f33-9dec-c3ce49ac87d8`)
was left deactivated (`isActive: false`) on both datasets rather than
deleted or reactivated — same reasoning as `EVALUATOR_CLEANUP.md`: no
`netra_delete_evaluator` MCP tool exists, so it stays orphaned like the
other known-dead evaluators. That Netra-side state was left as-is per
instruction to keep the dataset/evaluator state and revisit the code fix
later.

## Not fixed here

This is a defect in `netra-sdk`'s server-side expression evaluation, not
something fixable from this repo beyond avoiding the affected code path
entirely (as done above). Worth reporting to Netra with this document —
the three-variant reproduction above is enough for their engineers to
isolate exactly where in their expression pipeline the filter condition
gets dropped, without needing access to this project's own traces.
