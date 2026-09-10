# RCA: `netra-sdk` `run_test_suite()` crashes ~40% of items with "Event loop is closed"

## Impact

Running `eval/netra/run.py` against a 25-item dataset
(`cybersierra-morpheus-realistic-25`, `73cb309e-78ac-416d-b9ce-68fe6ad64a3f`,
2026-09-09) produced 10/24 items with `runStatus: "failed"` and
`taskOutput: null` — no agent output, no evaluator score, nothing to debug
from the dashboard alone. This is silent data loss: from the test-run
summary there is no way to distinguish "the agent genuinely failed" from
"the harness crashed before producing anything." It's plausible — and
consistent with the confirmed crash mechanism — that some of these crashes
happen after the agent already did the *correct* thing, destroying the
evidence before an evaluator score is recorded; see the caveat in
"Evidence" below on why this isn't proven by a specific example yet.

## Root cause

`netra-sdk==1.0.1`'s `Evaluation.run_test_suite()`
(`netra/evaluation/api.py`) does not honor the `max_concurrency` argument
the way its name implies, and combines that with an async resource that
isn't safe to share the way this code shares it.

1. **`max_concurrency` is floored at 5, silently.**
   `_run_test_suite_async` (`api.py`, inside `run_test_suite`):
   ```python
   max_workers = max(5, max_concurrency)
   ...
   with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
       await asyncio.gather(*[process_item(i, item) for i, item in enumerate(items)])
   ```
   `eval/netra/run.py` passes `max_concurrency=1` explicitly (with a comment
   describing it as "the cautious default"), but the executor is still
   built with 5 worker threads. With a 25-item dataset, up to 5 items run
   genuinely concurrently regardless of the caller's intent.

2. **Each worker thread opens and closes its own event loop, per item.**
   `process_item_sync` runs inside a `ThreadPoolExecutor` worker thread and
   calls `run_async_safely(self._process_single_item(...))`
   (`netra/evaluation/utils.py`). The real function (verified against the
   installed `netra-sdk==1.0.1` source, not paraphrased) is:
   ```python
   def run_async_safely(coroutine):
       try:
           loop = asyncio.get_running_loop()
       except RuntimeError:
           loop = None
       if loop and loop.is_running():
           # Spawns a dedicated thread and runs asyncio.run() there instead,
           # to avoid "asyncio.run() cannot be called from a running event
           # loop." This branch is real and does real work — it's just not
           # reachable here, because a freshly spawned ThreadPoolExecutor
           # worker thread has no event loop of its own to begin with.
           result_container = {}
           error_container = {}
           def _runner():
               try:
                   result_container["result"] = asyncio.run(coroutine)
               except Exception as exc:
                   error_container["error"] = exc
           thread = threading.Thread(target=_runner, daemon=True)
           thread.start()
           thread.join()
           if "error" in error_container:
               raise error_container["error"]
           return result_container.get("result")
       return asyncio.run(coroutine)
   ```
   Since `asyncio.get_running_loop()` raises in a fresh worker thread,
   `loop` is `None` and every item falls through to the final line:
   `asyncio.run(coroutine)` — which creates a brand-new event loop, runs
   the item's task, and **closes that loop** the moment the item finishes.
   With 5 worker threads, up to 5 independent event loops are alive at
   once, each with its own short lifecycle, torn down as soon as its item
   completes. (The conclusion here is unchanged from the original
   analysis — only the code quote was wrong; it previously omitted this
   branch and mischaracterized it as dead code.)

3. **Some resource is shared across those independently-created event
   loops — not yet confirmed which one.**

   The original version of this document blamed `Netra.init()`'s shared
   tracing/OTLP HTTP client. That attribution does not hold up and has
   been retracted:
   - Netra's span exporter is
     `opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter`,
     driven by `BatchSpanProcessor`'s own dedicated background **thread** —
     a synchronous, `requests`-based exporter with no relationship to any
     asyncio event loop. It cannot be broken by event-loop teardown.
   - Netra's REST client for dataset/run management
     (`netra.evaluation.client.EvaluationHttpClient`) is also a plain
     synchronous `httpx.Client`, not `AsyncClient`.
   - The harness's own LLM client isn't a long-lived shared object either:
     `harness/model.py` explicitly rebuilds it fresh on every call (its own
     docstring: "reads `AGENT_MODEL` fresh on every call rather than
     caching a module-level..."), and `harness/agent.py`'s `_build_agent()`
     is invoked "each time," per its own docstring. So there's no single
     persistent Anthropic client surviving across items either.

   What the crash evidence actually shows (see below) is the exception
   surfacing inside `anthropic/_streaming.py`'s async iteration — the
   **Anthropic SDK's own** async HTTP client, built on a separately
   vendored top-level package called `httpx2` (confirmed at
   `.venv/lib/python3.12/site-packages/httpx2`, a standalone dependency the
   installed `anthropic` package imports as `import httpx2` — **not** a
   submodule inside `netra/`'s own package tree).

   Since `_build_agent()` builds a fresh model/client per item, a
   *persistent* shared Anthropic client isn't the obvious explanation
   either — something else must be common across items despite that. The
   most concrete remaining candidate, not yet verified: `harness/agent.py`'s
   module-level `_checkpointer = InMemorySaver()`, which genuinely is one
   object shared by every `run()` call regardless of which event loop is
   asking. This needs a follow-up investigation before it goes to Netra as
   a confirmed root cause — right now, only "an async resource unsafe to
   share across independently-created-and-destroyed event loops" is
   confirmed; *which* resource is not.

## Evidence

- **The exception itself**, captured on `Agent_Turn` spans for crashed
  items (`agent.status: "failed"`, `has_error: true`):
  ```
  RuntimeError: Event loop is closed
    at harness/agent.py:409, in run() -> graph.astream_events(...)
    ... langchain_core/tracers/event_stream.py
    ... anthropic/_streaming.py (mid-response-stream, in one case)
  ```
- **The same error, independently, in process teardown**: background-task
  logs from these runs show `Task exception was never retrieved` /
  `RuntimeError: Event loop is closed` inside `httpx2/_client.py`'s
  `AsyncClient.aclose()`. **Correction:** the original version of this
  document described this as `netra/.../httpx2/_client.py`, implying it's
  part of netra's own package. Verified against the installed environment:
  `httpx2` is a separate, standalone top-level dependency
  (`.venv/lib/python3.12/site-packages/httpx2`), imported directly (
  `import httpx2`) by the `anthropic` package's `_base_client.py` — not
  nested inside `netra/` at all. This is the **Anthropic SDK's** own async
  client failing to close cleanly, not netra's.
- **The exact crash signature verified live**, but the specific item/trace
  citation below is unconfirmed and should be re-derived, not trusted —
  see the caveat that follows it.

  Fetching trace `02328cba2f4ee0362681db79710fb23c` directly
  (`netra_get_trace_by_id`) confirms this `RuntimeError: Event loop is
  closed` crash is real, at exactly `harness/agent.py:409` →
  `langchain_core/tracers/event_stream.py` → `anthropic/_streaming.py`,
  matching the stack trace above verbatim. **However**, that trace is
  rooted at `TestRun.smoke-test-cli-correctness-v3` — a multi-tool run
  with sub-agents and several different `cli_call` invocations — not a
  single-item run of "how many unread notifications do I have waiting?"
  against `cybersierra-morpheus-realistic-25`. It does not obviously
  correspond to dataset item `b2c114d8`, and its span attributes came back
  empty when re-fetched (likely because content capture wasn't enabled on
  whatever run produced it), so the specific "successful `cli_call` then a
  crash destroys the output" sequence could not be re-verified from this
  trace either way. **The trace ID was either pasted incorrectly, or this
  citation needs replacing with the actual trace for that item** — the
  original run's local results file that would have had the correct
  per-item trace ID has since been overwritten by later runs and is no
  longer recoverable. The underlying claim (a correctly-executed command's
  evidence gets destroyed by a same-signature crash before an evaluator
  score is recorded) is plausible and consistent with the confirmed crash
  mechanism, but is **not proven by this specific citation** — treat it as
  unconfirmed until re-derived from a fresh run's own results file.
- **Ruled out as the explanation for a different failure mode**: items that
  legitimately stop at the "Present Plan & Confirm" step (per
  `skills/cyber-sierra/SKILL.md`) always show `runStatus: "completed"` with
  a full, well-formed `taskOutput` — the opposite signature from the
  crashed items (`runStatus: "failed"`, `taskOutput: null`). The two
  failure modes are mutually exclusive in the data; this bug is not the
  cause of confirm-gate stalls, and confirm-gate stalls are not the cause
  of these crashes.

## Workaround applied (`eval/netra/run.py`)

`run_test_suite()` is now called once per dataset item (a 1-item `Dataset`
each time) inside a plain Python loop, instead of once with the whole
dataset. Since a worker-thread pool never has more than one real task
queued at a time, at most one event loop is ever alive — the concurrent
teardown race cannot happen regardless of what `netra-sdk` does internally
with `max_concurrency`.

Trade-offs accepted:
- **N separate Netra test runs instead of one.** Each item gets its own
  `runId`/dashboard entry, named `{run_name}-item-01` .. `{run_name}-item-NN`
  for grouping by name. `eval/netra/results/netra_run_summary.json` still
  collects all of them into one local file (`runIds` + `perItem`).
- **Slower wall-clock time.** No parallelism — a 25-item run takes roughly
  10-12 minutes instead of running 5-wide. Acceptable for this POC's
  dataset size; would need revisiting for a much larger dataset.

## Not fixed here

This is a bug in `netra-sdk`'s own concurrency handling — `max_concurrency`
is not honored, and its `ThreadPoolExecutor`-per-item design creates and
tears down independent event loops in a way that trips over *some* shared
async resource (confirmed as a real, live crash pattern; not yet confirmed
which resource). Not something fixable from this repo beyond avoiding the
buggy code path. Before reporting upstream to Netra: finish identifying the
actual shared resource (see the `_checkpointer` lead above) and replace the
`b2c114d8` trace citation with a verified one, so the report doesn't carry
an unconfirmed attribution or a mismatched example. Once fixed, the
workaround above can be reverted back to a single `run_test_suite()` call
over the whole dataset with real concurrency.
