"""Run the direct-service Netra track (2b): `Netra.evaluation.run_test_suite()`
against `eval/netra/task.py`'s in-process wrapper around `harness.agent.run()`
— no `/tracy/chat` hop, no morpheus_backend, no dedicated eval tenant. This
is the everyday replacement for `run_via_production_hop.py`'s role; that
script's real-production-hop testing is parked (see its own docstring and
`eval/README.md`'s "Two things both called 'the Netra track'" section).

Prerequisites:
    - `eval/netra/setup_dataset.py` has been run at least once (creates
      `.netra_eval_ids.json` with a `dataset_id`).
    - The Tool Correctness and Answer Relevance evaluators have been created
      and mapped to this dataset via the Netra dashboard (one-time, manual —
      see `eval/netra/EVALUATOR_SETUP.md`; this Netra instance doesn't
      support MCP, so that step can't be scripted the way it could be
      elsewhere). Running this script before that step just exercises the
      task/scoring path with no evaluator attached — useful as its own
      smoke test.

Usage:

    python -m eval.netra.run
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from netra import Netra
from netra.evaluation.models import Dataset

from harness.tracing import init_tracing

IDS_PATH = Path(__file__).resolve().parents[1] / "results" / ".netra_eval_ids.json"
SUMMARY_PATH = Path(__file__).resolve().parents[1] / "results" / "netra_run_summary.json"

APP_NAME = "cybersierra-deepagents-poc-eval"
RUN_NAME = "cybersierra-deepagents-poc-direct-run"

_TRUTHY = {"1", "true", "yes", "y", "on"}


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"{name} is required — see .env.example and eval/README.md")
    return value


def main(argv: list[str] | None = None) -> int:
    del argv  # no CLI options yet — kept for main(argv) parity with the other eval modules

    _require_env("NETRA_API_KEY")
    _require_env("NETRA_OTLP_ENDPOINT")
    if os.environ.get("NETRA_TRACING", "").strip().lower() not in _TRUTHY:
        sys.exit(
            "NETRA_TRACING must also be truthy for this track (not just NETRA_API_KEY/"
            "NETRA_OTLP_ENDPOINT) — this script needs REAL Agent_Turn/Plan_Step/CLI_Call "
            "spans captured in-process, the same gate harness/tracing.py's init_tracing() "
            "already enforces for the live server; without it there's nothing for the Tool "
            "Correctness evaluator to read via trace.tools."
        )

    if not IDS_PATH.exists():
        sys.exit(f"{IDS_PATH} not found — run `python -m eval.netra.setup_dataset` first.")
    ids = json.loads(IDS_PATH.read_text())
    dataset_id = ids.get("dataset_id")
    if not dataset_id:
        sys.exit(f"{IDS_PATH} has no dataset_id — run `python -m eval.netra.setup_dataset` first.")

    # One call does double duty: sets up Netra.evaluation (dataset/test-run
    # API) AND enables the live Agent_Turn/Plan_Step/CLI_Call spans
    # harness/agent.py and harness/sandbox.py create via Netra.start_span —
    # both are the same underlying Netra.init() call, just with this
    # module's own app_name instead of the live server's, so eval traces
    # stay visually separable in the dashboard.
    init_tracing(app_name=APP_NAME)
    if not getattr(Netra, "evaluation", None):
        sys.exit("Netra.evaluation failed to initialize — check NETRA_API_KEY/NETRA_OTLP_ENDPOINT/NETRA_TRACING.")

    print(f"Fetching dataset {dataset_id} ...")
    response = Netra.evaluation.get_dataset(dataset_id)
    if not response or not response.items:
        sys.exit(f"Netra.evaluation.get_dataset({dataset_id!r}) returned no items.")
    dataset = Dataset(items=response.items)
    print(f"{len(response.items)} item(s) loaded.")

    # Imported here, not at module top: harness.agent (transitively) reads
    # env-derived config at import time, so this stays deferred until after
    # this function's own env checks above have already run and failed fast
    # if something's missing.
    from eval.netra.task import run_task

    print(f"Running test suite {RUN_NAME!r} (max_concurrency=1) ...")
    # max_concurrency=1: deepagent-sdk-cli-poc's harness.agent.run() builds a
    # brand-new graph per call (see harness/agent.py's run()), so concurrency
    # is likely safe in principle, but this hasn't been verified under load —
    # same cautious default run_via_production_hop.py's original design used
    # for the same underlying single-process POC server.
    result = Netra.evaluation.run_test_suite(
        name=RUN_NAME,
        data=dataset,
        task=run_task,
        max_concurrency=1,
    )
    if not result:
        sys.exit("Netra.evaluation.run_test_suite returned no result — see logged error above.")

    run_id = result["runId"]
    print(f"Run id: {run_id}. Polling for results ...")

    run_results = Netra.evaluation.get_run_results(run_id)
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps({"runId": run_id, "items": result["items"], "results": run_results}, indent=2))
    print(f"Wrote {SUMMARY_PATH}")
    print(
        "\nView the Netra dashboard for Tool Correctness / Answer Relevance scores (once "
        "mapped per eval/netra/EVALUATOR_SETUP.md) alongside cost/latency."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
