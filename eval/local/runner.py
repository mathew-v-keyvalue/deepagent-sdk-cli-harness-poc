"""Run every query in `eval/local/dataset.json` against a REAL, already-
running `/chat` endpoint (not the harness function directly — this hits the
actual HTTP API, SSE parsing included, the same way a real client would),
and write the question/answer pairs (plus tool calls, token usage, timing)
to a results JSON.

Usage:

    uvicorn server.app:app &                    # start the server yourself first
    python -m eval.local.runner                 # defaults: http://localhost:8000,
                                                 # one example query per dataset
                                                 # entry, all four categories

    python -m eval.local.runner --base-url http://localhost:8000 \\
        --categories single,composite \\
        --output eval/results/my_run.json \\
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

Each result's `tool_calls` is a list of `{"name", "args"}` (not just names)
so `eval/local/scoring.py` can extract the actual invoked CLI commands from
`run_execution_plan`'s `args["plan_json"]` — see `eval/local/score.py`.

`auth_scenarios` entries (see `eval/local/dataset.json`) carry their own
`access_token` field, forwarded per-query — every other category sends none
(matching the harness's own "empty access_token = no per-request identity"
default, see `harness/agent.py`).

## Auto-confirm

`skills/cyber-sierra/SKILL.md`'s Step 4 ("Present Plan & Confirm")
requires confirmation before **every** execution, safe or not — "flag write
operations" there is just an annotation inside the presented plan, not a
condition on whether to ask at all (`harness/agent.py` has its own comment
confirming this was deliberately NOT overridden at the harness level).
Confirmed live: nearly every single-command query stops after presenting a
correct plan and asking "shall I proceed?", never reaching
`run_execution_plan` at all in that first turn — which would otherwise
score as a flat false negative on every query, regardless of how correct
the presented plan actually was.

So each query here is now up to two turns on the same session: if the
first turn's answer looks like a confirmation request (see
`eval/local/scoring.py`'s `looks_like_confirmation_request`) and it hasn't
already invoked `run_execution_plan`, a second turn sends a fixed "yes,
proceed" message on the same `session_id` (`resume`, not a new session —
see `harness/agent.py`'s `stream()`). Both turns' `tool_calls` are merged
before scoring, so `expected`/`expected_commands` are checked against
whatever was actually executed after confirming, not just what was
proposed. `result["auto_confirmed"]` records whether the second turn
happened at all.

## Concurrency

`--concurrency N` (default 1, i.e. today's serial behavior) runs up to N
queries at once via a `ThreadPoolExecutor` — `httpx.Client` is safe to share
across threads, and each query already has its own unique `session_id`
(`_eval_session_id`), so concurrent turns don't collide on server-side
session state. Verified live before adding this: 3 concurrent queries
against a real running server, different sessions, completed correctly
with no errors or cross-contamination, wall-clock time roughly equal to
the single slowest query instead of their sum. `--delay` is ignored when
`--concurrency` is greater than 1 (staggering serial requests and bounding
concurrent ones are different knobs — combining them doesn't mean
anything coherent). Results are still written incrementally as each
query completes (not batched at the end), guarded by a lock so concurrent
completions never interleave a corrupt write.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from eval.local.scoring import extract_actual_commands, looks_like_confirmation_request
from eval.shared.chat_client import run_one
from eval.shared.eval_identity import get_eval_access_token, resolve_access_token
from eval.shared.observability import configure_eval_logging

configure_eval_logging()
logger = logging.getLogger("eval.local.runner")

CONFIRM_MESSAGE = "Yes, please proceed with that plan."

DATASET_PATH = Path(__file__).resolve().parent / "dataset.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "results" / "results.json"

ALL_CATEGORIES = {"single", "composite", "adversarial", "auth"}

# Maps the full category key (as yielded by _iter_dataset_queries) to the
# short form used in the eval-tagged session_id below.
_CATEGORY_SHORT = {
    "single_command_queries": "single",
    "composite_queries": "composite",
    "adversarial_queries": "adversarial",
    "auth_scenarios": "auth",
}


def _eval_session_id(category: str, dataset_id: str) -> str:
    """A caller-chosen session_id, sent as /chat's `session_id` field so
    harness/agent.py's `stream()` uses it directly as the Netra session-
    grouping key (`Netra.set_session_id(thread_id)` where
    `thread_id = session_id or resume` — see harness/agent.py). Real
    frontend-originated sessions use morpheus_fe's own UUID scheme, which
    never has this `eval-local-` prefix — so in the Netra dashboard's
    session list, anything prefixed `eval-local-` is unambiguously this
    framework's traffic, not a real user, with no per-trace inspection
    needed."""
    short = _CATEGORY_SHORT.get(category, category)
    return f"eval-local-{short}-{dataset_id}-{uuid.uuid4().hex[:6]}"


def _iter_dataset_queries(dataset: dict, categories: set[str], all_examples: bool):
    """Yield (category, dataset_id, query_text, expected, access_token) for
    every query to run, per the --categories/--all-examples flags."""
    if "single" in categories:
        for entry in dataset["single_command_queries"]:
            queries = entry["example_queries"] if all_examples else entry["example_queries"][:1]
            for query in queries:
                yield (
                    "single_command_queries",
                    entry["id"],
                    query,
                    {"command": entry["command"], "safe": entry["safe"], "required_params": entry["required_params"]},
                    "",
                )
    if "composite" in categories:
        for entry in dataset["composite_queries"]:
            yield (
                "composite_queries",
                entry["id"],
                entry["example_query"],
                {"expected_commands": entry["expected_commands"]},
                "",
            )
    if "adversarial" in categories:
        for entry in dataset["adversarial_queries"]:
            yield (
                "adversarial_queries",
                entry["id"],
                entry["example_query"],
                {"expected_behavior": entry["expected_behavior"], "reason": entry["reason"]},
                "",
            )
    if "auth" in categories:
        for entry in dataset.get("auth_scenarios", []):
            yield (
                "auth_scenarios",
                entry["id"],
                entry["example_query"],
                {
                    "expected_commands": entry["expected_commands"],
                    "expected_behavior": entry.get("expected_behavior"),
                    "notes": entry.get("notes"),
                },
                entry.get("access_token", ""),
            )


def _load_existing(output_path: Path) -> dict[str, Any]:
    if output_path.exists():
        return json.loads(output_path.read_text())
    return {"generated_at": None, "base_url": None, "results": []}


def _already_done(existing: dict[str, Any], category: str, dataset_id: str, query: str) -> bool:
    return any(
        r["category"] == category and r["dataset_id"] == dataset_id and r["query"] == query
        for r in existing["results"]
    )


def _run_with_auto_confirm(
    client: httpx.Client,
    base_url: str,
    query: str,
    *,
    access_token: str,
    session_id: str,
) -> dict[str, Any]:
    """Run one query, then — if the first turn asked for confirmation
    without having invoked `run_execution_plan` — send a fixed "yes,
    proceed" second turn on the same `session_id` (see module docstring's
    "Auto-confirm" section for why this is needed at all). Returns a single
    merged outcome dict shaped like `run_one`'s, with `tool_calls`/`answer`
    combined across both turns and an added `auto_confirmed` flag."""
    first = run_one(client, base_url, query, access_token=access_token, session_id=session_id)

    already_executed = bool(extract_actual_commands(first.get("tool_calls") or []))
    needs_confirm = (
        not first["error"] and not already_executed and looks_like_confirmation_request(first.get("answer") or "")
    )
    if not needs_confirm:
        first["auto_confirmed"] = False
        return first

    second = run_one(client, base_url, CONFIRM_MESSAGE, access_token=access_token, session_id=session_id)

    merged = dict(second)
    merged["session_id"] = session_id
    merged["tool_calls"] = (first.get("tool_calls") or []) + (second.get("tool_calls") or [])
    merged["answer"] = (first.get("answer") or "") + "\n---CONFIRMED---\n" + (second.get("answer") or "")
    merged["duration_seconds"] = round((first.get("duration_seconds") or 0) + (second.get("duration_seconds") or 0), 2)
    merged["error"] = second.get("error") or first.get("error")
    merged["auto_confirmed"] = True
    merged["first_turn_answer"] = first.get("answer")
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000", help="Already-running /chat server (default: %(default)s)")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH, help="Path to dataset.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Where to write the question/answer JSON")
    parser.add_argument(
        "--categories",
        default="single,composite,adversarial,auth",
        help="Comma-separated subset of: single, composite, adversarial, auth (default: all four)",
    )
    parser.add_argument("--all-examples", action="store_true", help="Run every example_queries[] variant per single-command entry, not just the first")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N queries (for a quick smoke run)")
    parser.add_argument("--resume", action="store_true", help="Skip (category, dataset_id, query) triples already present in --output")
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds to sleep between queries (default: 0) — ignored if --concurrency > 1")
    parser.add_argument("--concurrency", type=int, default=1, help="Run up to N queries at once (default: 1, serial — see module docstring's 'Concurrency' section)")
    parser.add_argument(
        "--service-auth",
        default=os.environ.get("DEEPAGENT_SERVICE_AUTH"),
        help="X-Service-Auth value for the target server's mandatory service-to-service check "
        "(default: $DEEPAGENT_SERVICE_AUTH). /chat now 401s without this — see server/app.py.",
    )
    args = parser.parse_args(argv)
    if not args.service_auth:
        parser.error("--service-auth (or $DEEPAGENT_SERVICE_AUTH) is required — /chat rejects unauthenticated callers")

    categories = set(args.categories.split(","))
    if not categories <= ALL_CATEGORIES:
        parser.error(f"--categories must be a subset of {ALL_CATEGORIES}, got {categories}")

    dataset = json.loads(args.dataset.read_text())
    existing = _load_existing(args.output) if args.resume else {"generated_at": None, "base_url": None, "results": []}

    to_run = list(_iter_dataset_queries(dataset, categories, args.all_examples))
    if args.resume:
        to_run = [t for t in to_run if not _already_done(existing, t[0], t[1], t[2])]
    if args.limit is not None:
        to_run = to_run[: args.limit]

    eval_token = get_eval_access_token()
    if eval_token:
        print("Using a real per-request identity (dedicated eval tenant) for queries that don't specify their own.")
        logger.info(
            "run_start identity_mode=eval_tenant base_url=%s query_count=%d",
            args.base_url,
            len(to_run),
            extra={"event": "run_start", "identity_mode": "eval_tenant", "base_url": args.base_url, "query_count": len(to_run)},
        )
    else:
        print(
            "No eval tenant configured (EVAL_MORPHEUS_EMAIL/PASSWORD/ORG/CYBERSIERRA_BASE_URL) — "
            "falling back to the server host's persisted CLI profile, if any."
        )
        logger.info(
            "run_start identity_mode=persisted_profile_fallback base_url=%s query_count=%d",
            args.base_url,
            len(to_run),
            extra={"event": "run_start", "identity_mode": "persisted_profile_fallback", "base_url": args.base_url, "query_count": len(to_run)},
        )

    print(f"Running {len(to_run)} quer{'y' if len(to_run) == 1 else 'ies'} against {args.base_url} ...")

    try:
        health = httpx.get(f"{args.base_url}/health", timeout=5.0)
        health.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"FAIL: {args.base_url}/health did not respond ({exc}) — is the server running? "
              f"Start it with: uvicorn server.app:app")
        return 1

    results: list[dict[str, Any]] = existing["results"]
    results_lock = threading.Lock()
    completed = 0

    def _write_results_locked() -> None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {"generated_at": datetime.now(UTC).isoformat(), "base_url": args.base_url, "results": results},
                indent=2,
            )
        )

    def _run_one_task(client: httpx.Client, category: str, dataset_id: str, query: str, expected: dict, access_token: str) -> dict[str, Any]:
        resolved_token = resolve_access_token(access_token, eval_token)
        session_id = _eval_session_id(category, dataset_id)
        logger.info(
            "query_start category=%s dataset_id=%s session_id=%s using_own_token=%s",
            category,
            dataset_id,
            session_id,
            bool(access_token),  # True only for auth_scenarios entries carrying their own explicit token
            extra={
                "event": "query_start",
                "category": category,
                "dataset_id": dataset_id,
                "session_id": session_id,
                "using_own_token": bool(access_token),
            },
        )
        outcome = _run_with_auto_confirm(
            client,
            args.base_url,
            query,
            access_token=resolved_token,
            session_id=session_id,
        )
        logger.info(
            "query_done category=%s dataset_id=%s session_id=%s status=%s duration_seconds=%s tool_calls=%d auto_confirmed=%s error=%s",
            category,
            dataset_id,
            outcome.get("session_id"),
            "error" if outcome["error"] else "ok",
            outcome["duration_seconds"],
            len(outcome["tool_calls"]),
            outcome.get("auto_confirmed", False),
            outcome["error"],
            extra={
                "event": "query_done",
                "category": category,
                "dataset_id": dataset_id,
                "session_id": outcome.get("session_id"),
                "status": "error" if outcome["error"] else "ok",
                "duration_seconds": outcome["duration_seconds"],
                "tool_call_count": len(outcome["tool_calls"]),
                "auto_confirmed": outcome.get("auto_confirmed", False),
                "error": outcome["error"],
            },
        )
        return {
            "category": category,
            "dataset_id": dataset_id,
            "query": query,
            "expected": expected,
            "timestamp": datetime.now(UTC).isoformat(),
            **outcome,
        }

    with httpx.Client(headers={"X-Service-Auth": args.service_auth}) as client:
        if args.concurrency <= 1:
            # Serial path, unchanged from before concurrency existed —
            # --delay only makes sense here (staggering strictly one-at-a-
            # time requests), so it's honored only in this branch.
            for category, dataset_id, query, expected, access_token in to_run:
                completed += 1
                print(f"[{completed}/{len(to_run)}] ({category}/{dataset_id}) {query!r} ... ", end="", flush=True)
                result = _run_one_task(client, category, dataset_id, query, expected, access_token)
                status = "ERROR" if result["error"] else "ok"
                confirmed_note = " [auto-confirmed]" if result.get("auto_confirmed") else ""
                print(f"{status} ({result['duration_seconds']}s, {len(result['tool_calls'])} tool call(s)){confirmed_note}")
                results.append(result)
                _write_results_locked()
                if args.delay and completed < len(to_run):
                    time.sleep(args.delay)
        else:
            print(f"Running with concurrency={args.concurrency} ...")
            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                futures = {
                    executor.submit(_run_one_task, client, category, dataset_id, query, expected, access_token): (category, dataset_id, query)
                    for category, dataset_id, query, expected, access_token in to_run
                }
                for future in as_completed(futures):
                    category, dataset_id, query = futures[future]
                    result = future.result()
                    with results_lock:
                        completed += 1
                        status = "ERROR" if result["error"] else "ok"
                        confirmed_note = " [auto-confirmed]" if result.get("auto_confirmed") else ""
                        print(
                            f"[{completed}/{len(to_run)}] ({category}/{dataset_id}) {query!r} ... "
                            f"{status} ({result['duration_seconds']}s, {len(result['tool_calls'])} tool call(s)){confirmed_note}"
                        )
                        results.append(result)
                        _write_results_locked()

    ok_count = sum(1 for r in results if not r["error"])
    print(f"\nWrote {len(results)} result(s) to {args.output} ({ok_count} ok, {len(results) - ok_count} error(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
