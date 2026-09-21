"""Drift check, not a chat/harness test: every eval dataset's command
strings and `safe` flags actually match the live `cybersierra` CLI, AND
each dataset obeys its own write-operation invariant. Three files, two
opposite invariants:

  - `eval/local/dataset.json` and `eval/netra/dataset.json` (both
    independent, snapshot-style datasets — see their own `generated_from`/
    `_purpose` blocks) must contain **zero** write operations: every command
    they reference must be `safe:true`, live and in the dataset's own
    recorded flag. `eval/local/runner.py` and `eval/netra/task.py` both call
    the real agent against a real `cybersierra` backend, so a `safe:false`
    entry here is a real create/send/submit/update happening every run.

  - `eval/local/gate_scenarios.json` is the **inverse**: every entry must
    map to a command that's currently `safe:false`. These exist specifically
    to test that the Present-Plan-&-Confirm gate pauses before invoking a
    real write (`eval/local/gate_runner.py`) — if a live CLI update ever
    turns one of these commands read-only, the gate scenario is testing
    nothing and needs a different command swapped in.

The manifest genuinely changes over time (new modules/resources added by
`cybersierra self-update`, and in principle a command's `safe` flag could
flip in either direction) — both directions are checked here, before either
is discovered by actually running something.

Run it any time after `cybersierra self-update`, before adding a new
dataset entry, or periodically:

    python -m eval.verify_datasets
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

LOCAL_DATASET_PATH = Path(__file__).resolve().parent / "local" / "dataset.json"
GATE_SCENARIOS_PATH = Path(__file__).resolve().parent / "local" / "gate_scenarios.json"
NETRA_DATASET_PATH = Path(__file__).resolve().parent / "netra" / "dataset.json"


def _check_read_only_dataset(label: str, dataset: dict, live_by_command: dict) -> list[str]:
    """Shared check for eval/local/dataset.json and eval/netra/dataset.json:
    every referenced command must be safe:true, both in the dataset's own
    recorded flag and against the live manifest right now."""
    problems: list[str] = []

    for q in dataset.get("single_command_queries", []):
        cmd = q["command"]
        if not q["safe"]:
            problems.append(f"{label}.single_command_queries[{q['id']!r}]: command {cmd!r} is safe=false — write operations must not be in this dataset")

        live = live_by_command.get(cmd)
        if live is None:
            problems.append(f"{label}.single_command_queries[{q['id']!r}]: command {cmd!r} no longer exists in the live manifest")
            continue
        if live["safe"] != q["safe"]:
            problems.append(
                f"{label}.single_command_queries[{q['id']!r}]: dataset says safe={q['safe']} for {cmd!r}, "
                f"live manifest now says safe={live['safe']}"
                + (" -- a READ endpoint has become a WRITE endpoint; running this dataset would now perform a real write" if live["safe"] is False else "")
            )

    for q in dataset.get("composite_queries", []):
        for cmd in q["expected_commands"]:
            live = live_by_command.get(cmd)
            if live is None:
                problems.append(f"{label}.composite_queries[{q['id']!r}]: references {cmd!r}, which no longer exists in the live manifest")
            elif live["safe"] is False:
                problems.append(f"{label}.composite_queries[{q['id']!r}]: references {cmd!r}, which is now safe=false — write operations must not be in this dataset")

    for entry in dataset.get("auth_scenarios", []):
        cmd = entry["reuses_command"]
        live = live_by_command.get(cmd)
        if live is None:
            problems.append(f"{label}.auth_scenarios[{entry['id']!r}]: references {cmd!r}, which no longer exists in the live manifest")
        elif live["safe"] is False:
            problems.append(f"{label}.auth_scenarios[{entry['id']!r}]: references {cmd!r}, which is now safe=false — auth scenarios must reuse a read-only command")

    return problems


def _check_gate_scenarios(gate_data: dict, live_by_command: dict) -> list[str]:
    """Inverse invariant: every gate scenario's command must currently be
    safe:false. If one has flipped to safe:true, the scenario is no longer
    testing the confirm gate at all (there'd be nothing to confirm)."""
    problems: list[str] = []
    for entry in gate_data.get("gate_scenarios", []):
        cmd = entry["command"]
        if entry["safe"] is not False:
            problems.append(f"gate_scenarios[{entry['id']!r}]: command {cmd!r} is recorded as safe={entry['safe']!r} — gate scenarios must be safe:false by design")

        live = live_by_command.get(cmd)
        if live is None:
            problems.append(f"gate_scenarios[{entry['id']!r}]: command {cmd!r} no longer exists in the live manifest — swap in a current write command")
        elif live["safe"] is not False:
            problems.append(
                f"gate_scenarios[{entry['id']!r}]: {cmd!r} is now safe=true in the live manifest — this scenario no longer "
                "exercises the confirm gate (nothing to confirm); swap in a currently-unsafe command"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    del argv  # no CLI options — parity with the other eval modules

    try:
        manifest = json.loads(subprocess.check_output(["cybersierra", "manifest"], text=True))
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"FAIL: could not run 'cybersierra manifest': {exc}")
        return 1

    live_by_command = {e["command"]: e for e in manifest["data"]}

    local_dataset = json.loads(LOCAL_DATASET_PATH.read_text())
    netra_dataset = json.loads(NETRA_DATASET_PATH.read_text())
    gate_data = json.loads(GATE_SCENARIOS_PATH.read_text())

    problems = (
        _check_read_only_dataset("eval/local/dataset.json", local_dataset, live_by_command)
        + _check_read_only_dataset("eval/netra/dataset.json", netra_dataset, live_by_command)
        + _check_gate_scenarios(gate_data, live_by_command)
    )

    if problems:
        print(f"FAIL: {len(problems)} problem(s) found against manifest version {manifest['meta']['version']}:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(
        f"PASS: eval/local/dataset.json and eval/netra/dataset.json contain no write operations, "
        f"eval/local/gate_scenarios.json entries all still map to real write commands — "
        f"all against manifest version {manifest['meta']['version']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
