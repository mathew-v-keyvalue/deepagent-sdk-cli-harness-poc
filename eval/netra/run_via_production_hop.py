"""PARKED — not runnable in this environment (see `eval/README.md`'s "Two
things both called 'the Netra track'" section). This is the production-hop
path (2a): a real login, a real HTTP hop through morpheus_backend, real
cross-process OTEL trace propagation. It needs `MORPHEUS_BACKEND_URL` and a
dedicated eval tenant (`EVAL_MORPHEUS_EMAIL`/`PASSWORD`/`ORG`, MFA disabled)
that nobody has provisioned yet — flagged as a backend/devops ask, not
something to fake or route around here. Kept as working reference code for
whoever picks that up; for everyday use today, see `eval/netra/run.py`
(direct-service path, no tenant needed).

Run the Netra `tool_accuracy` eval against the dataset created by
`eval/netra/setup_dataset.py`, calling `/tracy/chat` on a real
morpheus_backend (which proxies to deepagent-sdk-cli-poc — see the v2
plan's Part A) rather than the deepagent server directly, and rather than
calling `harness.agent.run()` in-process.

**The one real risk this script exists to de-risk (plan's B3):** because
the call crosses a real HTTP hop into a separate process (morpheus_backend)
before reaching deepagent, OTEL trace context does NOT propagate for free
the way it would for an in-process call — `Agent_Turn`/`Plan_Step`/
`CLI_Call` spans would land in their own disconnected trace, invisible to
whatever `tool_accuracy` evaluator later reads `trace.tools` off THIS run's
`TestRun.*` span. `task()` below explicitly injects the current OTEL
context (`opentelemetry.propagate.inject`) into the outbound request
headers to fix that — do not remove this without re-verifying trace-linkage
(see "Verifying trace linkage" below).

Prerequisites (see eval/README.md and the original v2 plan's B0/B4):
    - `.netra_eval_ids.json` must exist (run `eval.netra.setup_dataset` first —
      note: that script points at `eval/netra/dataset.json` by default; this
      script's original design assumed the same ids file `eval/netra/run.py`
      also reads, so run only one of the two "run" scripts per dataset to
      avoid mixing item shapes).
    - A dedicated eval test tenant/user in morpheus (MFA disabled).
    - The `tool_accuracy` evaluator created and mapped to this dataset —
      ONLY after trace-linkage is confirmed (see below); running this
      script before that just exercises the task/scoring path with no
      evaluator attached, which is exactly what the smoke run is for.

Verifying trace linkage (do this once, on the smoke dataset, before
creating the evaluator): run this script, take the printed `trace_id` for
one item, and pull it with Netra MCP (`netra_get_trace_by_id`) or the
dashboard — `Agent_Turn`/`Plan_Step`/`CLI_Call` spans must appear in the
SAME trace as the `TestRun.*` span. If they don't, the evaluator will
silently see zero tools for every item — `eval/local/score.py` (which reads
the SSE capture directly, not `trace.tools`) stays correct regardless,
which is why that path exists independently of this one.

Usage:

    python -m eval.netra.run_via_production_hop
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from netra import Netra
from netra.evaluation.models import Dataset
from opentelemetry import propagate

from eval.shared.chat_client import parse_sse

IDS_PATH = Path(__file__).resolve().parents[1] / "results" / ".netra_eval_ids.json"
SUMMARY_PATH = Path(__file__).resolve().parents[1] / "results" / "netra_run_summary.json"

APP_NAME = "cybersierra-deepagents-poc-eval"
RUN_NAME = "cybersierra-deepagents-poc-tool-accuracy-run"

# 242 is morpheus/FusionAuth's MFA-required response code — this eval
# account must have MFA disabled (B0); if it doesn't, fail loudly and fast
# rather than hanging on a challenge no script can complete.
MFA_REQUIRED_CODE = 242


class _Env:
    """Small fail-fast env-var reader — every one of these is required for
    this script to do anything real, so there's no sensible partial-config
    default (same posture as server/app.py's DEEPAGENT_SERVICE_AUTH)."""

    def __init__(self) -> None:
        self.backend_url = self._require("MORPHEUS_BACKEND_URL").rstrip("/")
        self.chat_path = os.environ.get("DEEPAGENT_CHAT_PROXY_PATH", "/tracy/chat")
        self.email = self._require("EVAL_MORPHEUS_EMAIL")
        self.password = self._require("EVAL_MORPHEUS_PASSWORD")
        self.org = self._require("EVAL_MORPHEUS_ORG")
        self.netra_api_key = self._require("NETRA_API_KEY")
        self.netra_otlp_endpoint = self._require("NETRA_OTLP_ENDPOINT")

    @staticmethod
    def _require(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            sys.exit(f"{name} is required — see .env.example and eval/README.md")
        return value


class _MorpheusSession:
    """Scripted login, cached JWT, refreshed if close to expiry — mirrors
    what a real Tracy widget session does, minus the browser."""

    def __init__(self, env: _Env) -> None:
        self._env = env
        self._client = httpx.Client(timeout=30.0)
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self) -> str:
        # 60s safety margin rather than refreshing exactly at expiry —
        # avoids a token going stale mid-request on a slow /chat turn.
        if self._token is None or time.time() > self._expires_at - 60:
            self._login()
        return self._token  # type: ignore[return-value]

    def _login(self) -> None:
        resp = self._client.post(
            f"{self._env.backend_url}/api/v1/user_management/login/token",
            json={"email": self._env.email, "password": self._env.password, "org": self._env.org},
        )
        if resp.status_code == MFA_REQUIRED_CODE:
            sys.exit(
                f"Login returned {MFA_REQUIRED_CODE} (MFA required) — the eval test account "
                "(B0) must have MFA disabled. Aborting rather than hanging on a challenge "
                "this script can't complete."
            )
        resp.raise_for_status()
        data = resp.json()
        # ASSUMED response shape (token/expiresAt) — not yet confirmed
        # against a live morpheus_backend from this repo; adjust these two
        # field lookups against the real /login/token response the first
        # time this script is actually run.
        self._token = data["token"]
        self._expires_at = data.get("expiresAt") or (time.time() + 3600)


def _make_task(env: _Env, session: _MorpheusSession):
    """Build the `task(input_data)` callable `Netra.evaluation.run_test_suite`
    calls once per dataset item. `input_data` is the dataset item's `input`
    (the query string, per setup_netra_eval.py) — no session_id is minted
    here; letting the server mint one per query matches eval.local.runner's
    existing "independent one-shot questions" behavior (see its module
    docstring), not a multi-turn conversation."""

    def task(input_data: Any) -> dict[str, Any]:
        headers: dict[str, str] = {}
        # The concrete fix for the plan's B3 cross-process trace-linkage
        # risk: without this, Agent_Turn/Plan_Step/CLI_Call spans on the
        # deepagent side start a brand-new, disconnected trace instead of
        # continuing this TestRun's trace.
        propagate.inject(headers)

        start = time.monotonic()
        try:
            resp = session._client.post(
                f"{env.backend_url}{env.chat_path}",
                json={"message": input_data, "access_token": session.token()},
                headers=headers,
                timeout=180.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return {"tool_calls": [], "answer": "", "error": {"code": "http_error", "message": str(exc)},
                    "duration_seconds": round(time.monotonic() - start, 2)}

        tool_calls: list[dict[str, Any]] = []
        answer = ""
        error = None
        for event, data in parse_sse(resp.text):
            if event == "delta":
                answer += data["text"]
            elif event == "tool_use":
                tool_calls.append({"name": data["name"], "args": data.get("args") or {}})
            elif event == "error":
                error = data

        return {
            "tool_calls": tool_calls,
            "answer": answer,
            "error": error,
            "duration_seconds": round(time.monotonic() - start, 2),
        }

    return task


def main(argv: list[str] | None = None) -> int:
    del argv  # no CLI options — parity with the other eval modules
    env = _Env()

    if not IDS_PATH.exists():
        sys.exit(f"{IDS_PATH} not found — run `python -m eval.netra.setup_dataset` first.")
    ids = json.loads(IDS_PATH.read_text())
    dataset_id = ids.get("dataset_id")
    if not dataset_id:
        sys.exit(f"{IDS_PATH} has no dataset_id — run `python -m eval.netra.setup_dataset` first.")

    Netra.init(
        app_name=APP_NAME,
        headers=f"x-api-key={env.netra_api_key}",
        environment=os.environ.get("PLATFORM_ENV", "development"),
    )
    if not getattr(Netra, "evaluation", None):
        sys.exit("Netra.evaluation failed to initialize — check NETRA_API_KEY/NETRA_OTLP_ENDPOINT.")

    print(f"Fetching dataset {dataset_id} ...")
    response = Netra.evaluation.get_dataset(dataset_id)
    if not response or not response.items:
        sys.exit(f"Netra.evaluation.get_dataset({dataset_id!r}) returned no items.")
    dataset = Dataset(items=response.items)
    print(f"{len(response.items)} item(s) loaded.")

    session = _MorpheusSession(env)
    task = _make_task(env, session)

    print(f"Running test suite {RUN_NAME!r} (max_concurrency=1) ...")
    # max_concurrency=1: deepagent-sdk-cli-poc is a single-process,
    # in-memory-session POC server (server/sessions.py) — see the plan's
    # B2.7 note. Raise only after confirming concurrent turns behave.
    result = Netra.evaluation.run_test_suite(
        name=RUN_NAME,
        data=dataset,
        task=task,
        max_concurrency=1,
    )
    if not result:
        sys.exit("Netra.evaluation.run_test_suite returned no result — see logged error above.")

    run_id = result["runId"]
    print(f"Run id: {run_id}. Polling for results ...")

    run_results = Netra.evaluation.get_run_results(run_id)
    SUMMARY_PATH.write_text(json.dumps({"runId": run_id, "items": result["items"], "results": run_results}, indent=2))
    print(f"Wrote {SUMMARY_PATH}")
    print(
        "\nNext: pull one item's trace_id from the summary above and confirm "
        "Agent_Turn/Plan_Step/CLI_Call spans appear in the same trace as this "
        "run's TestRun.* span (netra_get_trace_by_id or the dashboard) before "
        "creating/trusting the tool_accuracy evaluator — see this script's docstring."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
