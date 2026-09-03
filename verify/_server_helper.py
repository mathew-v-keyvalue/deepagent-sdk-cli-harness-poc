"""Shared helpers for the verify/verify_server_*.py scripts. Not a test
itself. Modeled directly on the sibling Claude SDK POC's
`verify/_server_helper.py` — same two responsibilities: launch the real
server as a subprocess and tear it down, and parse an SSE response body
into (event, data, timestamp) tuples.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def running_server(port: int = 8098, timeout: float = 20.0, extra_env: dict[str, str] | None = None) -> Iterator[str]:
    """Launch `uvicorn server.app:app` as a real subprocess, poll `/health`
    until it responds, yield the base URL, then terminate it on exit.

    `extra_env` is merged on top of this process's own environment for the
    *server subprocess only* — e.g. `CYBERSIERRA_INJECT_ACCESS_TOKEN=1` for
    `verify_server_multi_session_isolation.py`'s env-isolation check, which
    needs that opt-in on (see `harness/agent.py`) without turning it on for
    every other verify script or this process itself.
    """
    base_url = f"http://127.0.0.1:{port}"
    env = {**os.environ, **(extra_env or {})}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app", "--port", str(port), "--log-level", "warning"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{base_url}/health", timeout=1.0)
                if resp.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        else:
            proc.terminate()
            raise RuntimeError(f"server did not become healthy on {base_url} within {timeout}s")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def read_sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, dict, float]]:
    """Parse an httpx streaming response's SSE body into
    `(event, data, monotonic_timestamp)` tuples, timestamped at the moment
    each event's terminating blank line is seen — used by
    verify_server_streaming_incremental.py to prove deltas arrive spread
    out over real wall-clock time, not all at once.
    """
    import json

    event_name = "message"
    data_lines: list[str] = []

    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
        elif line == "":
            if data_lines:
                yield event_name, json.loads("".join(data_lines)), time.monotonic()
            event_name = "message"
            data_lines = []
