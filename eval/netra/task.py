"""The task function `eval/netra/run.py` hands to
`Netra.evaluation.run_test_suite()` — a thin wrapper around
`harness.agent.run()`, called **in-process**, no HTTP hop, no running
`uvicorn server.app:app` needed.

This is the actual replacement for the old `run_via_production_hop.py`'s
role of "get a task output for Netra to score" — except that script went
through morpheus_backend's `/tracy/chat` proxy specifically to test the
real production wiring (parked — see that module's docstring and
`eval/README.md`). This task calls the harness directly, the same way the
SDK's own documented examples do, which is enough to get real
`Agent_Turn`/`Plan_Step`/`CLI_Call` spans under this run's `TestRun.*` span
(and therefore real `trace.tools` data for the Tool Correctness evaluator)
as long as `eval/netra/run.py` has already called `init_tracing(app_name=...)`
in this same process before `run_test_suite()` starts.
"""

from __future__ import annotations

from harness.agent import run as agent_run


async def run_task(input_data: str) -> str:
    """`run_test_suite` calls this once per dataset item with `item.input`
    (the query string — see `eval/netra/dataset.json`) and awaits it (task
    functions may be sync or async — see the installed SDK's
    `evaluation/utils.py::execute_task`). No `access_token` is forwarded
    here: this track scores general agent-answer quality (Answer Relevance,
    Tool Correctness), not per-user identity — that's `eval/local/
    dataset.json`'s `auth_scenarios`' job."""
    return await agent_run(input_data)
