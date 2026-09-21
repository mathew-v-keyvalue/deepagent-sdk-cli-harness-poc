"""Confirm-gate pause-only check: for every write-shaped query in
`eval/local/gate_scenarios.json`, send it as a single `/chat` turn and
verify the skill's Step 4 "Present Plan & Confirm" gate actually paused
instead of executing the write outright.

This is deliberately the *pause-only* half of confirm-gate coverage (see
`eval/README.md`'s "Confirm-gate scope" section) — it never sends a
follow-up "yes, proceed" turn, so running this file never causes a real
write against whatever backend `cybersierra` is pointed at. The full
confirm→execute→verify-write-happened test is parked pending a dedicated
write-capable test tenant.

A scenario passes only if BOTH hold:
  1. Zero tool_calls invoked its mapped write command (extracted the same
     way `eval/local/scoring.py` extracts any other command — via
     `run_execution_plan`'s `plan_json`, not bare `execute` discovery calls).
  2. The final answer text reads as asking for confirmation before
     proceeding (a keyword heuristic to start — see
     `eval.local.scoring.looks_like_confirmation_request`'s docstring for its limits).

Usage:

    uvicorn server.app:app &
    python -m eval.local.gate_runner --service-auth ...
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

from eval.local.scoring import extract_actual_commands, looks_like_confirmation_request
from eval.shared.chat_client import run_one
from eval.shared.eval_identity import get_eval_access_token
from eval.shared.observability import configure_eval_logging

configure_eval_logging()
logger = logging.getLogger("eval.local.gate_runner")

SCENARIOS_PATH = Path(__file__).resolve().parent / "gate_scenarios.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "results" / "gate_report.json"


def _eval_session_id(scenario_id: str) -> str:
    """Same convention as eval/local/runner.py's `_eval_session_id` — a
    caller-chosen session_id, `eval-gate-` prefixed, so gate-check traffic
    is just as unambiguously distinguishable from real frontend traffic in
    the Netra dashboard's session list."""
    return f"eval-gate-{scenario_id}-{uuid.uuid4().hex[:6]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000", help="Already-running /chat server (default: %(default)s)")
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS_PATH, help="Path to gate_scenarios.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Where to write the gate report JSON")
    parser.add_argument(
        "--service-auth",
        default=os.environ.get("DEEPAGENT_SERVICE_AUTH"),
        help="X-Service-Auth value (default: $DEEPAGENT_SERVICE_AUTH) — same requirement as eval.local.runner.",
    )
    args = parser.parse_args(argv)
    if not args.service_auth:
        parser.error("--service-auth (or $DEEPAGENT_SERVICE_AUTH) is required — /chat rejects unauthenticated callers")

    scenarios = json.loads(args.scenarios.read_text())["gate_scenarios"]

    try:
        health = httpx.get(f"{args.base_url}/health", timeout=5.0)
        health.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"FAIL: {args.base_url}/health did not respond ({exc}) — is the server running? "
              f"Start it with: uvicorn server.app:app")
        return 1

    eval_token = get_eval_access_token()
    print(
        "Using a real per-request identity (dedicated eval tenant)."
        if eval_token
        else "No eval tenant configured — falling back to the server host's persisted CLI profile, if any."
    )
    logger.info(
        "gate_run_start identity_mode=%s scenario_count=%d",
        "eval_tenant" if eval_token else "persisted_profile_fallback",
        len(scenarios),
        extra={
            "event": "gate_run_start",
            "identity_mode": "eval_tenant" if eval_token else "persisted_profile_fallback",
            "scenario_count": len(scenarios),
        },
    )

    report: list[dict[str, Any]] = []
    with httpx.Client(headers={"X-Service-Auth": args.service_auth}) as client:
        for i, scenario in enumerate(scenarios, 1):
            print(f"[{i}/{len(scenarios)}] ({scenario['id']}) {scenario['example_query']!r} ... ", end="", flush=True)
            outcome = run_one(
                client,
                args.base_url,
                scenario["example_query"],
                access_token=eval_token or "",
                session_id=_eval_session_id(scenario["id"]),
            )

            actual_commands = extract_actual_commands(outcome.get("tool_calls") or [])
            write_command_invoked = scenario["command"] in actual_commands
            asked_to_confirm = looks_like_confirmation_request(outcome.get("answer") or "")
            passed = (not write_command_invoked) and asked_to_confirm

            status = "PASS" if passed else "FAIL"
            print(status)
            logger.info(
                "gate_scenario_done id=%s session_id=%s status=%s write_command_invoked=%s asked_to_confirm=%s",
                scenario["id"],
                outcome.get("session_id"),
                status,
                write_command_invoked,
                asked_to_confirm,
                extra={
                    "event": "gate_scenario_done",
                    "id": scenario["id"],
                    "session_id": outcome.get("session_id"),
                    "status": status,
                    "write_command_invoked": write_command_invoked,
                    "asked_to_confirm": asked_to_confirm,
                },
            )

            report.append(
                {
                    "id": scenario["id"],
                    "session_id": outcome.get("session_id"),
                    "command": scenario["command"],
                    "query": scenario["example_query"],
                    "passed": passed,
                    "write_command_invoked": write_command_invoked,
                    "any_commands_invoked": actual_commands,
                    "asked_to_confirm": asked_to_confirm,
                    "answer": outcome.get("answer"),
                    "error": outcome.get("error"),
                }
            )

    passed_count = sum(1 for r in report if r["passed"])
    print(f"\n{passed_count}/{len(report)} scenario(s) passed (gate paused before writing, and asked to confirm).")
    for r in report:
        if not r["passed"]:
            reason = []
            if r["write_command_invoked"]:
                reason.append("WROTE WITHOUT CONFIRMATION -- write command was invoked")
            if not r["asked_to_confirm"]:
                reason.append("no confirmation-request phrasing detected (spot-check the answer by hand)")
            print(f"  FAIL [{r['id']}]: {'; '.join(reason)}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"passed": passed_count, "total": len(report), "results": report}, indent=2))
    print(f"\nWrote {args.output}")
    return 0 if passed_count == len(report) else 1


if __name__ == "__main__":
    sys.exit(main())
