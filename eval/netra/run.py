"""Run the direct-service Netra track (2b): `Netra.evaluation.run_test_suite()`
against `eval/netra/task.py`'s in-process wrapper around `harness.agent.run()`
— no `/tracy/chat` hop, no morpheus_backend, no dedicated eval tenant. This
is the everyday replacement for `run_via_production_hop.py`'s role; that
script's real-production-hop testing is parked (see its own docstring and
`eval/README.md`'s "Two things both called 'the Netra track'" section).

Prerequisites:
    - `eval/netra/setup_dataset.py` has been run at least once (creates
      `.netra_eval_ids.json` with a `dataset_id`), or pass --dataset-id for
      a dataset created directly via the Netra MCP tools.
    - Evaluators are mapped to the dataset/items via the Netra dashboard or
      MCP tools (`netra_map_evaluator_to_dataset`/`netra_update_dataset_item`)
      — see `eval/netra/EVALUATOR_SETUP.md`. Running this script before
      that step just exercises the task path with nothing scored.

Concurrency: this drives the dataset's items through Netra's own per-item
pipeline (`Evaluation._process_single_item`) directly, under one shared
`asyncio` event loop and a bounded `asyncio.Semaphore`, instead of calling
`Netra.evaluation.run_test_suite()` (which internally floors concurrency at
5 workers and gives each one its own throwaway event loop via
`asyncio.run()` — unsafe, see `eval/netra/NETRA_SDK_CONCURRENCY_RCA.md`'s
"Fixed" section for the full root cause and why this approach avoids it).

Usage:

    python -m eval.netra.run
    python -m eval.netra.run --dataset-id <id> --max-concurrency 3
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

from netra import Netra

from harness.tracing import init_tracing

IDS_PATH = Path(__file__).resolve().parents[1] / "results" / ".netra_eval_ids.json"
SUMMARY_PATH = Path(__file__).resolve().parents[1] / "results" / "netra_run_summary.json"

APP_NAME = "cybersierra-deepagents-poc-eval"
RUN_NAME = "cybersierra-deepagents-poc-direct-run"

_TRUTHY = {"1", "true", "yes", "y", "on"}

# Pinned deliberately: the concurrent runner below calls three
# underscore-prefixed Evaluation internals (verified against this exact
# installed version's source, see NETRA_SDK_CONCURRENCY_RCA.md) instead of
# the public (but unsafely concurrent) run_test_suite(). A netra-sdk
# upgrade must re-verify those internals still exist with the same
# behavior before this runner is trusted again — see
# _assert_netra_internals_compatible below, which fails loudly rather than
# silently reintroducing the old crash or silently breaking.
_EXPECTED_NETRA_SDK_VERSION = "1.0.1"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"{name} is required — see .env.example and eval/README.md")
    return value


def _assert_netra_internals_compatible() -> None:
    installed = importlib.metadata.version("netra-sdk")
    if installed != _EXPECTED_NETRA_SDK_VERSION:
        sys.exit(
            f"netra-sdk {installed} is installed, but this concurrent runner's internals were "
            f"verified against netra-sdk=={_EXPECTED_NETRA_SDK_VERSION} only (see "
            "eval/netra/NETRA_SDK_CONCURRENCY_RCA.md). Re-check whether Evaluation._process_single_item/"
            "create_run/_client.post_run_status still exist with the same behavior in the new version "
            "before running this — an upstream fix to run_test_suite()'s own concurrency may mean this "
            "whole workaround can be deleted instead."
        )
    evaluation = Netra.evaluation
    missing = [
        name
        for name in ("_process_single_item", "create_run", "_client")
        if not hasattr(evaluation, name)
    ]
    if not missing and not hasattr(evaluation._client, "post_run_status"):
        missing.append("_client.post_run_status")
    if missing:
        sys.exit(
            f"netra.evaluation.Evaluation is missing internal(s) this concurrent runner depends on: "
            f"{missing!r}. See eval/netra/NETRA_SDK_CONCURRENCY_RCA.md."
        )


async def _run_dataset(
    dataset_id: str, run_name: str, items: list[Any], max_concurrency: int, task: Any
) -> dict[str, Any] | None:
    """Run every item in `items` through Netra's own per-item pipeline
    concurrently, under one shared event loop and a bounded semaphore —
    the fix for the run_test_suite() concurrency bug, see module docstring
    and NETRA_SDK_CONCURRENCY_RCA.md. Reuses Evaluation._process_single_item
    (span creation, task execution, result posting) exactly as
    run_test_suite() does internally; only the scheduling differs.

    evaluators=None throughout: this repo never passes SDK-local evaluators
    to run_test_suite() either — every evaluator here is configured
    server-side (dashboard/MCP) and triggers automatically once a
    TestRunItem is posted, so Evaluation._run_evaluators_for_item() (a
    separate, unused-by-us feature) isn't needed.
    """
    evaluation = Netra.evaluation
    run_id = evaluation.create_run(name=run_name, dataset_id=dataset_id)
    if not run_id:
        sys.exit("netra.evaluation.create_run() failed — see logs above.")

    semaphore = asyncio.Semaphore(max_concurrency)
    completed = 0
    total = len(items)

    async def process_one(idx: int, item: Any) -> dict[str, Any]:
        nonlocal completed
        async with semaphore:
            result = await evaluation._process_single_item(idx, item, run_id, run_name, task, None)
        completed += 1
        print(f"[{completed}/{total}] item {idx + 1} -> status={result.status}")
        return result.item_entry

    items_result = await asyncio.gather(*(process_one(i, item) for i, item in enumerate(items)))
    evaluation._client.post_run_status(run_id, "completed")
    return {"runId": run_id, "items": list(items_result)}


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
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=3,
        help="How many items to run concurrently (default: 3 — conservative starting point for a "
        "concurrency-bug workaround, see NETRA_SDK_CONCURRENCY_RCA.md; raise once proven solid).",
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
    _assert_netra_internals_compatible()

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

    print(
        f"Running {len(items)} item(s) under run name {run_name!r}, "
        f"max {args.max_concurrency} concurrent ..."
    )
    result = asyncio.run(_run_dataset(dataset_id, run_name, items, args.max_concurrency, run_task))
    if not result:
        sys.exit("_run_dataset returned no result — see logged error above.")
    run_id = result["runId"]
    run_results = Netra.evaluation.get_run_results(run_id)

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps({"runIds": [run_id], "perItem": [{"runId": run_id, "items": result["items"], "results": run_results}]}, indent=2)
    )
    print(f"Wrote {summary_path}")
    print(
        f"\n{len(result['items'])}/{len(items)} item(s) completed under one Netra test run "
        f"({run_name!r}, id {run_id}) — view the dashboard for evaluator scores alongside cost/latency."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
