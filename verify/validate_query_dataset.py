"""dataset/query_dataset.json's command strings and safe flags actually
match the live cybersierra CLI, AND the dataset contains no write
operations at all.

This is a drift check, not a chat/harness test: `dataset/query_dataset.json`
is a snapshot (see its own `generated_from` block), and the CLI genuinely
changes — the manifest grew from 3 modules/8 commands (when this repo's
sandbox allowlist and skill-porting work was first done) to 5 modules/37
commands (`notifications` and several `tprm` resources — `activity-logs`,
`assessee-scans`, `assessee-tenant-users` — were added by a `cybersierra
self-update` in between, observed directly in this session). A dataset
entry pointing at a command that no longer exists is one drift failure
mode; a command whose `safe` flag flipped is a second, more dangerous one
in *either* direction: `false -> true` just makes a dataset entry stale,
but `true -> false` (a read endpoint later gaining a destructive side
effect) means `dataset/run_dataset.py` could now run a real write against
whatever backend it's pointed at, from an entry that was safe when it was
written. Both are checked here, before either is discovered by actually
running the dataset.

The no-write-operations rule is enforced as a hard invariant (not just a
one-time cleanup): `dataset/run_dataset.py` calls the real `/chat` endpoint
against a real `cybersierra` backend, so any `safe:false` entry in this
dataset is a real create/send/submit/update happening every time someone
runs it — on purpose removed, and this script fails loudly if one ever
comes back (a future manifest update flipping a flag, or someone adding a
new entry without checking `safe` first).

Run it any time after `cybersierra self-update`, before adding a new
dataset entry, or periodically:

    python verify/validate_query_dataset.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

DATASET_PATH = Path(__file__).resolve().parent.parent / "dataset" / "query_dataset.json"


def main() -> int:
    try:
        manifest = json.loads(subprocess.check_output(["cybersierra", "manifest"], text=True))
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"FAIL: could not run 'cybersierra manifest': {exc}")
        return 1

    live_by_command = {e["command"]: e for e in manifest["data"]}
    dataset = json.loads(DATASET_PATH.read_text())

    problems: list[str] = []

    for q in dataset["single_command_queries"]:
        cmd = q["command"]

        # Hard invariant, independent of the live manifest: this dataset
        # must never contain a write operation. Checked on the dataset's
        # own recorded `safe` flag first so this still fires even if the
        # command has since vanished from the manifest entirely (checked
        # below too, separately).
        if not q["safe"]:
            problems.append(f"single_command_queries[{q['id']!r}]: command {cmd!r} is safe=false — write operations must not be in this dataset")

        live = live_by_command.get(cmd)
        if live is None:
            problems.append(f"single_command_queries[{q['id']!r}]: command {cmd!r} no longer exists in the live manifest")
            continue
        if live["safe"] != q["safe"]:
            problems.append(
                f"single_command_queries[{q['id']!r}]: dataset says safe={q['safe']} for {cmd!r}, "
                f"live manifest now says safe={live['safe']}"
                + (" -- a READ endpoint has become a WRITE endpoint; dataset/run_dataset.py would now perform a real write" if live["safe"] is False else "")
            )

    for q in dataset["composite_queries"]:
        for cmd in q["expected_commands"]:
            live = live_by_command.get(cmd)
            if live is None:
                problems.append(f"composite_queries[{q['id']!r}]: references {cmd!r}, which no longer exists in the live manifest")
            elif live["safe"] is False:
                problems.append(f"composite_queries[{q['id']!r}]: references {cmd!r}, which is now safe=false — write operations must not be in this dataset")

    live_safe_count = sum(1 for e in manifest["data"] if e["safe"])
    live_count = len(manifest["data"])
    dataset_count = len(dataset["single_command_queries"])

    if problems:
        print(f"FAIL: {len(problems)} problem(s) found against manifest version {manifest['meta']['version']}:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"PASS: every dataset command string and safe flag matches manifest version {manifest['meta']['version']}; no write operations present")
    if live_safe_count != dataset_count:
        print(
            f"NOTE: live manifest has {live_safe_count} read-only (safe=true) commands out of {live_count} total, "
            f"dataset covers {dataset_count} of them — no mismatches, just commands this dataset doesn't have "
            "example queries for yet (or, if dataset_count > live_safe_count, ones the manifest has since removed)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
