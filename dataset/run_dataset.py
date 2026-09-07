"""Run every query in dataset/query_dataset.json against a REAL, already-
running /chat endpoint (not the harness function directly — this hits the
actual HTTP API, SSE parsing included, the same way a real client would),
and write the question/answer pairs (plus tool calls, token usage, timing)
to a results JSON.

Usage:

    uvicorn server.app:app &            # start the server yourself first
    python dataset/run_dataset.py       # defaults: http://localhost:8000,
                                         # one example query per dataset
                                         # entry, all three categories

    python dataset/run_dataset.py --base-url http://localhost:8000 \\
        --categories single,composite \\
        --output dataset/results/my_run.json \\
        --all-examples \\
        --resume

Each dataset entry gets its own fresh session (no session_id sent) — these
are independent one-shot questions, not a multi-turn conversation; a
composite query is still one /chat call, since "list X then get detail on
the first one" is meant to become one Canonical Execution Plan with
multiple steps, not multiple chat turns.

Writes incrementally (one line of progress + a full-file rewrite after each
query) so a long run interrupted partway through doesn't lose completed
results, and --resume skips (query, category) pairs already present in an
existing output file rather than re-asking (and re-paying for) them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DATASET_PATH = Path(__file__).resolve().parent / "query_dataset.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "results.json"


def _iter_dataset_queries(dataset: dict, categories: set[str], all_examples: bool):
    """Yield (category, dataset_id, query_text, expected) for every query
    to run, per the --categories/--all-examples flags."""
    if "single" in categories:
        for entry in dataset["single_command_queries"]:
            queries = entry["example_queries"] if all_examples else entry["example_queries"][:1]
            for query in queries:
                yield (
                    "single_command_queries",
                    entry["id"],
                    query,
                    {"command": entry["command"], "safe": entry["safe"], "required_params": entry["required_params"]},
                )
    if "composite" in categories:
        for entry in dataset["composite_queries"]:
            yield ("composite_queries", entry["id"], entry["example_query"], {"expected_commands": entry["expected_commands"]})
    if "adversarial" in categories:
        for entry in dataset["adversarial_queries"]:
            yield (
                "adversarial_queries",
                entry["id"],
                entry["example_query"],
                {"expected_behavior": entry["expected_behavior"], "reason": entry["reason"]},
            )


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse a complete (non-streaming-read) SSE response body — the
    dataset run doesn't need incremental timing the way
    verify_server_streaming_incremental.py does, just the final events."""
    events = []
    event_name = "message"
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
        elif line == "":
            if data_lines:
                events.append((event_name, json.loads("".join(data_lines))))
            event_name = "message"
            data_lines = []
    if data_lines:
        events.append((event_name, json.loads("".join(data_lines))))
    return events


def _run_one(client: httpx.Client, base_url: str, query: str) -> dict[str, Any]:
    start = time.monotonic()
    result: dict[str, Any] = {
        "answer": "",
        "tool_calls": [],
        "session_id": None,
        "subtype": None,
        "total_cost_usd": None,
        "usage": None,
        "num_turns": None,
        "error": None,
    }
    try:
        resp = client.post(f"{base_url}/chat", data={"message": query}, timeout=180.0)
        resp.raise_for_status()
        for event, data in _parse_sse(resp.text):
            if event == "session":
                result["session_id"] = data["session_id"]
            elif event == "delta":
                result["answer"] += data["text"]
            elif event == "tool_use":
                result["tool_calls"].append(data["name"])
            elif event == "done":
                result["subtype"] = data.get("subtype")
                result["total_cost_usd"] = data.get("total_cost_usd")
                result["usage"] = data.get("usage")
                result["num_turns"] = data.get("num_turns")
            elif event == "error":
                result["error"] = data
    except httpx.HTTPError as exc:
        result["error"] = {"code": "http_error", "message": str(exc)}

    result["duration_seconds"] = round(time.monotonic() - start, 2)
    return result


def _load_existing(output_path: Path) -> dict[str, Any]:
    if output_path.exists():
        return json.loads(output_path.read_text())
    return {"generated_at": None, "base_url": None, "results": []}


def _already_done(existing: dict[str, Any], category: str, dataset_id: str, query: str) -> bool:
    return any(
        r["category"] == category and r["dataset_id"] == dataset_id and r["query"] == query
        for r in existing["results"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000", help="Already-running /chat server (default: %(default)s)")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH, help="Path to query_dataset.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Where to write the question/answer JSON")
    parser.add_argument(
        "--categories",
        default="single,composite,adversarial",
        help="Comma-separated subset of: single, composite, adversarial (default: all three)",
    )
    parser.add_argument("--all-examples", action="store_true", help="Run every example_queries[] variant per single-command entry, not just the first")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N queries (for a quick smoke run)")
    parser.add_argument("--resume", action="store_true", help="Skip (category, dataset_id, query) triples already present in --output")
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds to sleep between queries (default: 0)")
    parser.add_argument(
        "--service-auth",
        default=os.environ.get("DEEPAGENT_SERVICE_AUTH"),
        help="X-Service-Auth value for the target server's mandatory service-to-service check "
        "(default: $DEEPAGENT_SERVICE_AUTH). /chat now 401s without this — see server/app.py.",
    )
    args = parser.parse_args()
    if not args.service_auth:
        parser.error("--service-auth (or $DEEPAGENT_SERVICE_AUTH) is required — /chat rejects unauthenticated callers")

    categories = set(args.categories.split(","))
    valid = {"single", "composite", "adversarial"}
    if not categories <= valid:
        parser.error(f"--categories must be a subset of {valid}, got {categories}")

    dataset = json.loads(args.dataset.read_text())
    existing = _load_existing(args.output) if args.resume else {"generated_at": None, "base_url": None, "results": []}

    to_run = list(_iter_dataset_queries(dataset, categories, args.all_examples))
    if args.resume:
        to_run = [t for t in to_run if not _already_done(existing, t[0], t[1], t[2])]
    if args.limit is not None:
        to_run = to_run[: args.limit]

    print(f"Running {len(to_run)} quer{'y' if len(to_run) == 1 else 'ies'} against {args.base_url} ...")

    try:
        health = httpx.get(f"{args.base_url}/health", timeout=5.0)
        health.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"FAIL: {args.base_url}/health did not respond ({exc}) — is the server running? "
              f"Start it with: uvicorn server.app:app")
        return 1

    results: list[dict[str, Any]] = existing["results"]
    with httpx.Client(headers={"X-Service-Auth": args.service_auth}) as client:
        for i, (category, dataset_id, query, expected) in enumerate(to_run, 1):
            print(f"[{i}/{len(to_run)}] ({category}/{dataset_id}) {query!r} ... ", end="", flush=True)
            outcome = _run_one(client, args.base_url, query)
            status = "ERROR" if outcome["error"] else "ok"
            print(f"{status} ({outcome['duration_seconds']}s, {len(outcome['tool_calls'])} tool call(s))")

            results.append(
                {
                    "category": category,
                    "dataset_id": dataset_id,
                    "query": query,
                    "expected": expected,
                    "timestamp": datetime.now(UTC).isoformat(),
                    **outcome,
                }
            )

            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(
                    {"generated_at": datetime.now(UTC).isoformat(), "base_url": args.base_url, "results": results},
                    indent=2,
                )
            )

            if args.delay and i < len(to_run):
                time.sleep(args.delay)

    ok_count = sum(1 for r in results if not r["error"])
    print(f"\nWrote {len(results)} result(s) to {args.output} ({ok_count} ok, {len(results) - ok_count} error(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
