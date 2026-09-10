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

import argparse
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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dataset-id",
        default=None,
        help="Netra dataset id to run against, bypassing the .netra_eval_ids.json lookup "
        "(e.g. a dataset created directly via the Netra MCP tools instead of setup_dataset.py).",
    )
    parser.add_argument(
        "--run-name",
        default=None,
        help=f"Name for this test run (default: {RUN_NAME!r}).",
    )
    parser.add_argument(
        "--summary-path",
        type=Path,
        default=None,
        help=f"Where to write the run summary JSON (default: {SUMMARY_PATH}).",
    )
    args = parser.parse_args(argv)

    run_name = args.run_name or RUN_NAME
    summary_path = args.summary_path or SUMMARY_PATH

    _require_env("NETRA_API_KEY")
    _require_env("NETRA_OTLP_ENDPOINT")
    if os.environ.get("NETRA_TRACING", "").strip().lower() not in _TRUTHY:
        sys.exit(
            "NETRA_TRACING must also be truthy for this track (not just NETRA_API_KEY/"
            "NETRA_OTLP_ENDPOINT) — this script needs REAL Agent_Turn/Plan_Step/cli_call "
            "spans captured in-process, the same gate harness/tracing.py's init_tracing() "
            "already enforces for the live server; without it there's nothing for the Tool "
            "Correctness evaluator to read via trace.tools."
        )

    if args.dataset_id:
        dataset_id = args.dataset_id
    else:
        if not IDS_PATH.exists():
            sys.exit(f"{IDS_PATH} not found — run `python -m eval.netra.setup_dataset` first, or pass --dataset-id.")
        ids = json.loads(IDS_PATH.read_text())
        dataset_id = ids.get("dataset_id")
        if not dataset_id:
            sys.exit(f"{IDS_PATH} has no dataset_id — run `python -m eval.netra.setup_dataset` first, or pass --dataset-id.")

    # One call does double duty: sets up Netra.evaluation (dataset/test-run
    # API) AND enables the live Agent_Turn/Plan_Step/cli_call spans
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
    items = list(response.items)
    print(f"{len(items)} item(s) loaded.")

    # Imported here, not at module top: harness.agent (transitively) reads
    # env-derived config at import time, so this stays deferred until after
    # this function's own env checks above have already run and failed fast
    # if something's missing.
    from eval.netra.task import run_task

    # One item per Netra.evaluation.run_test_suite() call, never the whole
    # dataset at once — max_concurrency is NOT honored by netra-sdk 1.0.1
    # (it floors worker count at 5 regardless), and each worker thread opens
    # its own event loop via asyncio.run(). Something shared across those
    # independently-created-and-destroyed event loops isn't safe to touch
    # concurrently that way — exact resource not yet confirmed, see
    # eval/netra/NETRA_SDK_CONCURRENCY_RCA.md. Symptom: ~40% of items crash
    # with "RuntimeError: Event loop is closed" mid-run, silently dropping
    # real results. Submitting one item at a time means at most one worker
    # thread/event loop is ever alive, which eliminates the race regardless
    # of which resource turns out to be at fault.
    print(f"Running {len(items)} item(s) one at a time under run name {run_name!r} ...")
    run_ids: list[str] = []
    per_item_results: list[dict] = []
    for idx, item in enumerate(items, start=1):
        item_run_name = f"{run_name}-item-{idx:02d}"
        print(f"[{idx}/{len(items)}] {item_run_name} ...")
        result = Netra.evaluation.run_test_suite(
            name=item_run_name,
            data=Dataset(items=[item]),
            task=run_task,
            max_concurrency=1,
        )
        if not result:
            print(f"  WARN: run_test_suite returned no result for item {idx} — see logged error above, continuing")
            continue
        run_id = result["runId"]
        run_ids.append(run_id)
        run_results = Netra.evaluation.get_run_results(run_id)
        per_item_results.append({"runId": run_id, "items": result["items"], "results": run_results})

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({"runIds": run_ids, "perItem": per_item_results}, indent=2))
    print(f"Wrote {summary_path}")
    print(
        f"\n{len(run_ids)}/{len(items)} item(s) completed. Each item is its own Netra test run "
        f"(names {run_name}-item-01 .. {run_name}-item-{len(items):02d}) — view the dashboard for "
        "Tool Correctness / Answer Relevance scores (once mapped per eval/netra/EVALUATOR_SETUP.md) "
        "alongside cost/latency."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
