"""Shared SSE-parsing/`/chat`-calling helpers, used by `eval/local/runner.py`
(the main scored dataset), `eval/local/gate_runner.py` (the confirm-gate
pause-only check — needs the same request/parse shape, plus a second,
same-`session_id` turn), and `eval/netra/run_via_production_hop.py` (the
parked production-hop path, which still imports `parse_sse` for its own
differently-shaped request). Split out of the original `run_dataset.py`
verbatim (no behavior change) so every caller parses the same SSE event
vocabulary the same way instead of maintaining separate copies.

`run_one`'s request shape (form-encoded body, `X-Service-Auth` header) is
specific to calling deepagent's `/chat` directly. `run_via_production_hop.py`
calls a different endpoint (morpheus_backend's `/tracy/chat`, JSON body,
forwarded JWT, explicit OTEL trace-context propagation) and builds its own
request around the shared `parse_sse` instead.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx


def parse_sse(text: str) -> list[tuple[str, dict]]:
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


def run_one(
    client: httpx.Client,
    base_url: str,
    query: str,
    *,
    access_token: str = "",
    session_id: str | None = None,
) -> dict[str, Any]:
    """POST one query straight to deepagent's `/chat` (form-encoded,
    `X-Service-Auth` on `client`) and parse the SSE response. Each `tool_call`
    entry keeps the full `{"name": ..., "args": ...}` shape (not just the
    name) so `eval/local/scoring.py` can extract actually-invoked commands
    from `run_execution_plan` calls.

    `access_token` is forwarded as-is (empty string = omitted field, matching
    `/chat`'s own default — see server/app.py) so per-request identity
    scenarios (`eval/local/dataset.json`'s `auth_scenarios`) can exercise the
    real `MORPHEUS_TOKEN` injection path. `session_id`, when given, resumes
    an existing session instead of starting a new one — this is what lets
    `eval/local/gate_runner.py` send a second, same-session "yes, proceed"
    turn after the first turn's response (a fresh call omits it, matching
    every existing dataset scenario's "independent one-shot question"
    behavior)."""
    start = time.monotonic()
    result: dict[str, Any] = {
        "answer": "",
        "tool_calls": [],
        "session_id": session_id,
        "subtype": None,
        "total_cost_usd": None,
        "usage": None,
        "num_turns": None,
        "error": None,
    }
    data: dict[str, str] = {"message": query}
    if access_token:
        data["access_token"] = access_token
    if session_id:
        data["session_id"] = session_id
    try:
        resp = client.post(f"{base_url}/chat", data=data, timeout=180.0)
        resp.raise_for_status()
        for event, event_data in parse_sse(resp.text):
            if event == "session":
                result["session_id"] = event_data["session_id"]
            elif event == "delta":
                result["answer"] += event_data["text"]
            elif event == "tool_use":
                result["tool_calls"].append({"name": event_data["name"], "args": event_data.get("args") or {}})
            elif event == "done":
                result["session_id"] = event_data.get("session_id") or result["session_id"]
                result["subtype"] = event_data.get("subtype")
                result["total_cost_usd"] = event_data.get("total_cost_usd")
                result["usage"] = event_data.get("usage")
                result["num_turns"] = event_data.get("num_turns")
            elif event == "error":
                result["error"] = event_data
    except httpx.HTTPError as exc:
        result["error"] = {"code": "http_error", "message": str(exc)}

    result["duration_seconds"] = round(time.monotonic() - start, 2)
    return result
