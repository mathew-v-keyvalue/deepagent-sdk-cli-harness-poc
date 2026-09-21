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

## Addendum (2026-09-11) — a second, different-shaped instance: single-match filter, function-wrapped

The bug above was reproduced against **multi-match** `spans[?name=='cli_call']`
filters. On 2026-09-11, a related-but-distinct failure showed up on a
**single-match** filter (`spans[?name=='Agent_Turn']` — there is always
exactly one such span per trace, unlike `cli_call`), while setting up the
Hallucination evaluator's `retrieved_context` variable (see
`EVALUATOR_FINDINGS.md`'s 2026-09-11 update for the full evaluator-setup
context).

**Setup:** a new `agent.actual_outputs` list[str] attribute was added to
the `Agent_Turn` span (`harness/agent.py`, mirroring the already-proven
`agent.actual_commands` attribute — see that file's new `on_tool_end`
handler). Verified directly on multiple live traces: this attribute is
genuinely populated with the real CLI output text every time a command
actually executes.

**Expression tried:** `join('\n', spans[?name=='Agent_Turn'].agent.actual_outputs[])`
— no pipe (an earlier pipe-based attempt, `spans[?name=='Agent_Turn'] |
[0].agent.actual_outputs | join('\n', @)`, was rejected outright by the
map-time validator as invalid JMESPath — pipes nested before a function
call don't seem to be supported at all, syntactically, regardless of the
runtime bug below). The no-pipe, flatten-then-join form **does** pass the
map-time validator.

**Result:** on every live test run tried (multiple different dataset
items, including ones where the trace-verified `agent.actual_outputs`
attribute was genuinely non-empty and the agent's final answer visibly
used that real data), the Hallucination evaluator's reasoning consistently
said context was null/absent ("Context is null", "no context provided to
support them") — i.e. `retrieved_context` resolved empty despite: (a) the
source attribute being correctly populated (confirmed by reading the span
directly), and (b) the mapping expression passing the map-time validator.

**Ruled out:** this is not the "bucket: user_provided" variable-definition
tag routing `retrieved_context` through a different resolution path (e.g.
`evaluatorConfigs`, the mechanism used for Guideline Adherence's
`assistant_instructions`) instead of `variableMapping` — tested directly:
mapping `retrieved_context` to a bare string literal (no `spans`/`input`/
etc. reference) was rejected by the API with an explicit error
("`retrieved_context` expression must reference one of: input, taskOutput,
expectedOutput, trace, spans, metadata, turns, voiceConversation"),
confirming `variableMapping` **is** the required and validated mechanism
for this variable, not a red herring.

**Not confirmed:** whether this specific no-pipe flatten form has the exact
same root cause as the multi-match pipe-form bug documented above (a
filter/projection not really applied at runtime), or a distinct issue
specific to the flatten (`[]`) operator, or something about `join()`
consuming a projected/flattened array specifically. Not isolated further —
see "Decision" below.

**Follow-up isolation (2026-09-11, same day) — decisive, and cheap:**
rather than keep re-running the full paid agent to test each new
expression variant, a standalone script
(`debug_hallucination_mapping.py`, scratchpad-only, not committed)
replicated `netra.evaluation.api.Evaluation._execute_item_pipeline`
directly — opening the same `SpanWrapper`/`Agent_Turn` span structure and
posting a completed item — but with `execute_task()` (the expensive real
agent call) replaced by instantly-returned, already-known-real data
copied from a previous live trace. This cost only the evaluator's own
tiny judge call (~$0.008) per iteration instead of a full agent run
(~$0.03–0.2), and let several mapping variants be tested in minutes for
under $0.03 total. Results, in order:

1. `retrieved_context: {"expression": "spans[?name=='Agent_Turn'].agent.actual_outputs[]"}` (join-wrapped) → null context, "fabricated" verdict.
2. `retrieved_context: {"expression": "spans[?name=='Agent_Turn'] | [0].agent.actual_outputs"}` (the exact pipe form proven to work for `agent.actual_commands` elsewhere) → still null.
3. **Control test:** `retrieved_context: {"expression": "taskOutput"}` → **resolved correctly** (score 1, "All claims perfectly match provided context verbatim") — proves the `retrieved_context` variable slot and `variableMapping` mechanism both work fine in general for this evaluator; the bucket="user_provided" tag is not a blocker (also separately ruled out: mapping to a bare string literal is explicitly rejected by the API's own validator, confirming `variableMapping` is the required, enforced mechanism, not a red herring).
4. **Simplest possible spans probe:** `retrieved_context: {"expression": "spans[0].name"}` (no filter, no field-chasing, just the first span's plain `name` string) → first attempt hit an unrelated cause (the org's configured Anthropic provider credit balance ran out mid-investigation, `400: Your credit balance is too low`) before the judge could run at all — inconclusive, not a real result. **Re-run once credits were restored (same day):** real evaluation ran (cost $0.0082, not an error) and **still resolved to empty context** ("context provides no supporting information") — confirming this isn't a fluke of the credit outage; the trivial unfiltered form fails too.

**Correction (same day, caught via direct dashboard inspection of the "Evaluated
Variables" panel — not just the judge's prose "reason"):** the `spans[0].name` test above
was misread. It did **not** fail — it correctly resolved to
`"TestRun.debug-hallucination-mapping-no-agent"`, the *root* wrapper span (index 0 in the
trace's span array), not the `Agent_Turn` span. This proves `spans` as a root **does**
resolve fine for `llm-as-judge` evaluators in general — the earlier "spans fails broadly"
conclusion was wrong, reached by trusting the judge's natural-language "reason" text
("context is null...") as if it reported the literal resolved value, rather than checking
the actual resolved value directly. Lesson: always check the dashboard's "Evaluated
Variables" panel (or an equivalent raw value) directly — the judge's reasoning text is not a
reliable proxy for what a variable actually resolved to.

**What's actually still unexplained:** `spans[?name=='Agent_Turn'] | [0].agent.actual_outputs`
— the exact same pipe-filter pattern proven to work for `agent.actual_commands` on other
evaluator types — still produces a judge response indicating no usable context, even
against a minimal, hand-verified 2-span trace (`TestRun.*` → `Agent_Turn`, confirmed via
direct trace inspection) where the `Agent_Turn` span is genuinely present with real,
correctly-shaped `agent.actual_outputs` data. Root cause not yet confirmed at the literal
resolved-value level for this specific filtered case (time-boxed — see decision below); it
may be filter-matching, or something size/content-specific to this attribute, or something
else — genuinely unknown as of this writing.

**Decision:** Topic Adherence and Plan Quality (both verified working, real
non-null scores, no `spans` dependency in their mappings) shipped as-is on
2026-09-11. Hallucination was **dropped** (deactivated dataset-wide,
removed from all 5 item-level overrides) rather than shipped with a
fake/meaningless `retrieved_context` mapping (e.g. pointing it at
`taskOutput` itself would make the evaluator check the answer against
itself — not a real hallucination check). Worth reporting to Netra with
this file as-is — the isolation above (spans fails on llm-as-judge in
every shape, works fine on regex/code, and taskOutput works fine on the
same llm-as-judge evaluator) is specific enough for their engineers to
act on without needing access to this project's own traces.
