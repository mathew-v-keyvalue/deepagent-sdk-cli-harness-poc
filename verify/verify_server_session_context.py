"""Chat server: session context actually carries across requests.

Modeled directly on the sibling Claude SDK POC's
verify_server_session_context.py: starts the real server, POSTs "My name is
Ada Lovelace" with no session_id, captures the returned one, POSTs "What is
my name?" with that session_id, asserts the second answer mentions "Ada".

Deliberately a plain conversational exchange, not a cybersierra question —
this isolates the session-continuity mechanism itself (here: DeepAgents'
checkpointer + thread_id, see harness/agent.py) from anything about the
cybersierra CLI. No real token needed; a syntactically-present placeholder
satisfies the SSE contract's `access_token` requirement without ever
reaching a real cybersierra backend call.

Needs a real model API key (ANTHROPIC_API_KEY or the alt provider
configured via AGENT_MODEL/OPENAI_API_KEY) to actually run — unlike
verify_shell_sandbox_denies.py, there is no honest way to script this one
without a live model, since the thing being tested is the model actually
recalling "Ada" from a prior turn.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from _server_helper import running_server

PLACEHOLDER_TOKEN = "placeholder-token-not-real"


async def main() -> int:
    with running_server(port=8099) as base_url:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp1 = await client.post(
                f"{base_url}/chat",
                data={"message": "My name is Ada Lovelace.", "access_token": PLACEHOLDER_TOKEN},
            )
            if resp1.status_code != 200:
                print(f"FAIL: turn 1 returned {resp1.status_code}: {resp1.text}")
                return 1

            session_id = None
            for line in resp1.text.splitlines():
                if line.startswith("data:") and '"session_id"' in line:
                    import json

                    session_id = json.loads(line[len("data:") :].strip()).get("session_id")
                    break
            if not session_id:
                print(f"FAIL: no session event in turn 1 response:\n{resp1.text[:2000]}")
                return 1
            print(f"turn 1 ok, session_id={session_id}")

            resp2 = await client.post(
                f"{base_url}/chat",
                data={
                    "message": "What is my name? Answer with just the name.",
                    "session_id": session_id,
                    "access_token": PLACEHOLDER_TOKEN,
                },
            )
            if resp2.status_code != 200:
                print(f"FAIL: turn 2 returned {resp2.status_code}: {resp2.text}")
                return 1

            full_text = ""
            for line in resp2.text.splitlines():
                if line.startswith("data:"):
                    import json

                    try:
                        payload = json.loads(line[len("data:") :].strip())
                    except json.JSONDecodeError:
                        continue
                    if "text" in payload:
                        full_text += payload["text"]

            print(f"turn 2 answer: {full_text!r}")
            if "ada" not in full_text.lower():
                print("FAIL: second turn did not recall the name from turn 1 — session context did not carry")
                return 1

            print("PASS: session context carried across two /chat requests via the checkpointer + thread_id")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
