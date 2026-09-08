"""One-time, re-runnable setup: create (or extend) a Netra evaluation
dataset from `eval/netra/dataset.json` (an independent dataset definition —
see that file's own `_purpose`, and `eval/README.md`'s "Two independent
dataset definitions" section for why this deliberately isn't derived from
`eval/local/dataset.json`), so `eval/netra/run.py` has a `dataset_id` to run
against and a `tool_accuracy` evaluator can be mapped onto it.

Start with a small **smoke dataset** (default — one single/composite/
adversarial item) and confirm results look sane before scaling to the full
set with `--full`. Creating the library evaluators themselves (Tool
Correctness, Answer Relevance) is a one-time manual dashboard step, not
scripted here — see `eval/netra/EVALUATOR_SETUP.md` (this Netra instance
doesn't support MCP, so that step can't be automated the way it could be
elsewhere).

Uses a distinct `app_name` (`cybersierra-deepagents-poc-eval`) from the
live server's (`cybersierra-deepagents-poc`) so eval traces are visually
separable in the Netra dashboard.

Usage:

    python -m eval.netra.setup_dataset            # smoke dataset (3 items)
    python -m eval.netra.setup_dataset --full      # all items in dataset.json

Re-running is safe: items already recorded in `.netra_eval_ids.json` (by
`category:dataset_id` key) are skipped rather than re-added, and the
existing Netra dataset is reused (by `dataset_name`) rather than creating a
duplicate one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from netra import Netra
from netra.evaluation.models import DatasetItem, TurnType

DATASET_PATH = Path(__file__).resolve().parent / "dataset.json"
IDS_PATH = Path(__file__).resolve().parents[1] / "results" / ".netra_eval_ids.json"

APP_NAME = "cybersierra-deepagents-poc-eval"
DATASET_NAME = "cybersierra-deepagents-poc-tool-accuracy"

# One single/composite/adversarial item, per B4 step 3-4's "validate on
# 2-3 manual runs, one of each shape" before trusting the pipeline at scale.
SMOKE_ITEM_KEYS = {
    ("single_command_queries", "health-status-get"),
    ("composite_queries", "composite-snapshot"),
    ("adversarial_queries", "adversarial-email-report"),
}


def _init_netra() -> None:
    api_key = os.environ.get("NETRA_API_KEY")
    otlp_endpoint = os.environ.get("NETRA_OTLP_ENDPOINT")
    if not api_key or not otlp_endpoint:
        sys.exit(
            "NETRA_API_KEY and NETRA_OTLP_ENDPOINT are both required to talk to Netra's "
            "evaluation API (see .env.example) — refusing to run with tracing/eval silently "
            "disabled, since a dataset 'created' against a no-op client isn't real."
        )
    Netra.init(
        app_name=APP_NAME,
        headers=f"x-api-key={api_key}",
        environment=os.environ.get("PLATFORM_ENV", "development"),
    )
    if not getattr(Netra, "evaluation", None):
        sys.exit("Netra.evaluation failed to initialize — check NETRA_API_KEY/NETRA_OTLP_ENDPOINT.")


def _iter_entries(dataset: dict[str, Any], item_keys: set[tuple[str, str]] | None):
    """Yield (category, dataset_id, input_text, expected_commands) for every
    dataset entry to create, using the first example query per single-
    command entry (matches eval.local.runner's default, non---all-examples
    behavior — one representative query per manifest command, not every
    phrasing)."""
    for entry in dataset["single_command_queries"]:
        key = ("single_command_queries", entry["id"])
        if item_keys is not None and key not in item_keys:
            continue
        yield ("single_command_queries", entry["id"], entry["example_queries"][0], [entry["command"]])
    for entry in dataset["composite_queries"]:
        key = ("composite_queries", entry["id"])
        if item_keys is not None and key not in item_keys:
            continue
        yield ("composite_queries", entry["id"], entry["example_query"], list(entry["expected_commands"]))
    for entry in dataset["adversarial_queries"]:
        key = ("adversarial_queries", entry["id"])
        if item_keys is not None and key not in item_keys:
            continue
        yield ("adversarial_queries", entry["id"], entry["example_query"], [])


def _load_ids() -> dict[str, Any]:
    if IDS_PATH.exists():
        return json.loads(IDS_PATH.read_text())
    return {"dataset_name": DATASET_NAME, "dataset_id": None, "items": {}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--full", action="store_true", help="Create every dataset.json item instead of the 3-item smoke set")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH, help="Path to eval/netra/dataset.json")
    args = parser.parse_args(argv)

    _init_netra()

    query_dataset = json.loads(args.dataset.read_text())
    ids = _load_ids()

    if ids["dataset_id"] is None:
        response = Netra.evaluation.create_dataset(name=DATASET_NAME, turn_type=TurnType.SINGLE)
        if not response or not response.id:
            sys.exit("Netra.evaluation.create_dataset failed — see logged error above.")
        ids["dataset_id"] = response.id
        print(f"Created dataset {DATASET_NAME!r} ({response.id})")
    else:
        print(f"Reusing existing dataset {ids['dataset_name']!r} ({ids['dataset_id']})")

    item_keys = None if args.full else SMOKE_ITEM_KEYS
    created = 0
    for category, dataset_id, query, expected_commands in _iter_entries(query_dataset, item_keys):
        key = f"{category}:{dataset_id}"
        if key in ids["items"]:
            continue

        item = DatasetItem(
            input=query,
            expected_output=json.dumps(expected_commands),
            # "tools" duplicates expected_output's content under the key
            # name Tool Correctness's library defaultVariableMapping
            # actually points at (metadata.tools, not expectedOutput — see
            # eval/netra/EVALUATOR_SETUP.md) so either mapping works without
            # editing dataset items after the fact.
            metadata={"category": category, "dataset_id": dataset_id, "tools": expected_commands},
        )
        response = Netra.evaluation.add_dataset_item(dataset_id=ids["dataset_id"], item=item)
        if not response or not response.id:
            print(f"WARN: failed to add item {key!r} — see logged error above, continuing")
            continue
        ids["items"][key] = response.id
        created += 1
        print(f"[{created}] added {key!r} -> {response.id}")

        IDS_PATH.write_text(json.dumps(ids, indent=2))

    print(f"\n{created} item(s) created this run, {len(ids['items'])} total recorded in {IDS_PATH}.")
    print(f"Dataset id: {ids['dataset_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
