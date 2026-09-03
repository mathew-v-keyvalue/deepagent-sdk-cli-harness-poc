"""Chat server: concurrent sessions stay isolated.

Two independent checks, both always runnable (neither needs a real
cybersierra login — see README "Authentication model" for why this POC
doesn't assume one), only a real model API key:

1. `check_two_sessions_distinct_env_tokens` — proves the thing the sibling
   Claude SDK POC's `verify_multi_user_isolation.py` needed two real,
   distinct backend tokens to prove: that one concurrent request's
   `access_token` never leaks into another's tool-call environment. We can
   prove this WITHOUT real cybersierra credentials, because the mechanism
   under test is `harness.agent._build_agent`'s per-call `env=` injection
   (see harness/sandbox.py's `AllowlistedShellBackend`), and `python3` is
   itself on the shell allowlist — so we ask each session to run
   `python3 -c "...os.environ.get('CYBERSIERRA_TOKEN')..."` via the
   sandboxed `execute` tool and report back exactly what it printed. Two
   concurrent sessions, two distinct marker tokens, and we assert each
   session's final answer contains only its own marker. This is a stronger,
   fully-automatable substitute for needing two real user accounts — it is
   the harness's own env-scoping being exercised for real, not mocked.

   This check specifically needs `CYBERSIERRA_INJECT_ACCESS_TOKEN=1` on the
   server it starts — see `harness/agent.py`'s `_build_agent` — since that
   injection is opt-in, off by default (so a UI placeholder token doesn't
   clobber an already-`cybersierra auth login-browser`'d session). We set
   it only in this subprocess's own environment, only for this one server
   instance, not globally.

2. `check_two_sessions_same_token_different_facts` — modeled directly on
   the Claude POC's same-named check: same placeholder token on two
   different concurrent session_ids (one told "my favorite color is red,"
   the other "blue"), asserting neither leaks into the other. This is a
   different property than check 1 — conversation-state isolation (the
   checkpointer's thread_id scoping), not env isolation — and needs its own
   test because a harness could get one right and the other wrong.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

import httpx

from _server_helper import running_server

PORT = 8100


async def _chat(client: httpx.AsyncClient, base_url: str, message: str, access_token: str) -> tuple[str, str]:
    resp = await client.post(f"{base_url}/chat", data={"message": message, "access_token": access_token})
    resp.raise_for_status()
    full_text = ""
    session_id = None
    for line in resp.text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[len("data:") :].strip())
        except json.JSONDecodeError:
            continue
        if "text" in payload:
            full_text += payload["text"]
        if "session_id" in payload and session_id is None:
            session_id = payload["session_id"]
    return full_text, session_id


async def check_two_sessions_distinct_env_tokens(client: httpx.AsyncClient, base_url: str) -> bool:
    token_a = f"marker-A-{uuid.uuid4().hex[:8]}"
    token_b = f"marker-B-{uuid.uuid4().hex[:8]}"
    prompt = (
        "Run exactly this shell command with the execute tool: "
        "python3 -c \"import os; print('TOKEN_IS_' + os.environ.get('CYBERSIERRA_TOKEN', 'MISSING'))\" "
        "Then tell me, in your final answer, exactly what it printed to stdout."
    )

    (answer_a, _), (answer_b, _) = await asyncio.gather(
        _chat(client, base_url, prompt, token_a),
        _chat(client, base_url, prompt, token_b),
    )

    print(f"session A (token={token_a}) answered: {answer_a!r}")
    print(f"session B (token={token_b}) answered: {answer_b!r}")

    if token_a not in answer_a:
        print("FAIL: session A's own token never appeared in its answer — env injection may be broken")
        return False
    if token_b not in answer_b:
        print("FAIL: session B's own token never appeared in its answer — env injection may be broken")
        return False
    if token_b in answer_a:
        print("FAIL: session B's token leaked into session A's subprocess environment")
        return False
    if token_a in answer_b:
        print("FAIL: session A's token leaked into session B's subprocess environment")
        return False

    print("PASS: two concurrent sessions each saw only their own access_token in CYBERSIERRA_TOKEN")
    return True


def _extract_text(resp: httpx.Response) -> str:
    text = ""
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            try:
                payload = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                continue
            if "text" in payload:
                text += payload["text"]
    return text


async def check_two_sessions_same_token_different_facts(client: httpx.AsyncClient, base_url: str) -> bool:
    """Modeled directly on the Claude POC's same-named check: same
    placeholder token, two concurrent new session_ids told different facts,
    then a follow-up on each proving neither leaked into the other. This
    exercises conversation-state isolation (the checkpointer's thread_id
    scoping) — a different property than `check_two_sessions_distinct_env_tokens`,
    which exercises env isolation; a harness could get one right and the
    other wrong, so both get their own check.
    """
    shared_token = "placeholder-token-not-real"

    (_, sid_red), (_, sid_blue) = await asyncio.gather(
        _chat(client, base_url, "My favorite color is red. Just acknowledge briefly.", shared_token),
        _chat(client, base_url, "My favorite color is blue. Just acknowledge briefly.", shared_token),
    )
    if not sid_red or not sid_blue or sid_red == sid_blue:
        print(f"FAIL: expected two distinct session ids, got {sid_red!r} and {sid_blue!r}")
        return False

    follow_red, follow_blue = await asyncio.gather(
        client.post(
            f"{base_url}/chat",
            data={"message": "What is my favorite color? One word.", "session_id": sid_red, "access_token": shared_token},
        ),
        client.post(
            f"{base_url}/chat",
            data={"message": "What is my favorite color? One word.", "session_id": sid_blue, "access_token": shared_token},
        ),
    )

    answer_red = _extract_text(follow_red).lower()
    answer_blue = _extract_text(follow_blue).lower()
    print(f"session RED, asked again: {answer_red!r}")
    print(f"session BLUE, asked again: {answer_blue!r}")

    ok = True
    if "red" not in answer_red or "blue" in answer_red:
        print("FAIL: RED session did not recall red (or leaked blue)")
        ok = False
    if "blue" not in answer_blue or "red" in answer_blue:
        print("FAIL: BLUE session did not recall blue (or leaked red)")
        ok = False
    if ok:
        print("PASS: two concurrent sessions on the same access_token kept independent conversation state")
    return ok


async def main() -> int:
    with running_server(port=PORT, extra_env={"CYBERSIERRA_INJECT_ACCESS_TOKEN": "1"}) as base_url:
        async with httpx.AsyncClient(timeout=90.0) as client:
            ok1 = await check_two_sessions_distinct_env_tokens(client, base_url)
            ok2 = await check_two_sessions_same_token_different_facts(client, base_url)

    if ok1 and ok2:
        print("\nPASS: concurrent sessions stay isolated on both axes (env token, conversation state)")
        return 0
    print("\nFAIL: see above")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
