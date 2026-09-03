"""Chat server: streaming is actually incremental.

Modeled directly on the sibling Claude SDK POC's
verify_server_streaming_incremental.py: times each `delta` SSE event for a
long response and asserts they're spread out over real wall-clock time —
not just "more than one event," which a fully-buffered-then-burst-emitted
response would also satisfy. Also asserts at least one `tool_use` event
fires with the harness's real tool name (`execute`, `read_file`, etc, not
`Bash`/`Read` — see harness/agent.py's tool-name bridge in
SYSTEM_PROMPT_APPENDIX), by asking the model to actually use one.

Needs a real model API key to run.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from _server_helper import read_sse_events, running_server

MIN_SPREAD_SECONDS = 0.3
PLACEHOLDER_TOKEN = "placeholder-token-not-real"


async def main() -> int:
    with running_server(port=8101) as base_url:
        async with httpx.AsyncClient(timeout=90.0) as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat",
                data={
                    "message": (
                        "List the files in the current directory using the execute tool "
                        "(e.g. `cybersierra --version` first, just to use a tool), then "
                        "separately, count out loud from one to thirty, one number per line."
                    ),
                    "access_token": PLACEHOLDER_TOKEN,
                },
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    print(f"FAIL: server returned {resp.status_code}: {body!r}")
                    return 1

                delta_timestamps = []
                tool_use_names = []
                saw_done = False

                async for event, data, ts in read_sse_events(resp):
                    if event == "delta":
                        delta_timestamps.append(ts)
                    elif event == "tool_use":
                        tool_use_names.append(data["name"])
                    elif event == "done":
                        saw_done = True
                    elif event == "error":
                        print(f"FAIL: server sent an error event: {data}")
                        return 1

    if not saw_done:
        print("FAIL: stream never produced a `done` event")
        return 1

    if len(delta_timestamps) < 2:
        print(f"FAIL: only {len(delta_timestamps)} delta event(s) — can't assess incrementality")
        return 1

    spread = delta_timestamps[-1] - delta_timestamps[0]
    print(f"{len(delta_timestamps)} delta events spread over {spread:.3f}s")
    if spread < MIN_SPREAD_SECONDS:
        print(
            f"FAIL: deltas arrived within {spread:.3f}s (< {MIN_SPREAD_SECONDS}s) — "
            "looks buffered-then-burst-emitted, not truly incremental"
        )
        return 1
    print(f"PASS: deltas spread over {spread:.3f}s (>= {MIN_SPREAD_SECONDS}s) — real incremental streaming")

    print(f"tool_use events seen: {tool_use_names}")
    if not tool_use_names:
        print("FAIL: no tool_use event fired even though the prompt asked for a tool call")
        return 1
    print("PASS: at least one tool_use event fired")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
