"""Chat server: works on a genuinely fresh deployment host, with zero
persisted CLI profile and zero interactive login, ever.

This is the regression test for a real gap found live (not by reading code
and assuming it worked): `harness/agent.py` forwarded a per-request
`MORPHEUS_TOKEN` override but never forwarded a base URL at all. On a host
where `~/.cybersierra/config.json` has never been created — exactly what a
real, freshly-provisioned multi-tenant deployment looks like, since no
human ever manually logs in on that box — every single CLI call failed
with `"No baseUrl configured"`, regardless of how correct the forwarded
token was. See README "Authentication model" (Fix 3) for the full story.

Needs a real model key — launches the real server, asks it to run one real
CLI command, and inspects the actual answer text. Does NOT need a real
`cybersierra` login anywhere: `HOME` is pointed at a freshly created,
genuinely empty temp directory (no `.cybersierra` subdirectory at all, not
just an emptied config file), so there is no persisted profile of any kind
for the CLI to fall back on. `access_token` is a placeholder, not a real
JWT — this test is about whether base-URL resolution works, not about
getting a real successful backend call, so a real rejection ("Invalid
token") counts as success here, same as it did for the token-naming fix.

Two checks:

1. **Positive**: `CYBERSIERRA_BASE_URL` set on the server → the answer must
   NOT contain "No baseUrl configured". Whatever the CLI actually returns
   (a real "Invalid token" rejection, since the token here is a
   placeholder) proves the request got *past* base-URL resolution and
   reached the real backend.
2. **Negative control**: same fresh `HOME`, `CYBERSIERRA_BASE_URL` deliberately
   left unset → must reproduce the original failure ("No baseUrl
   configured" appears in the answer). Without this, check 1 passing
   wouldn't prove the fix does anything — it could just as easily pass if
   this whole mechanism were a no-op.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

from _server_helper import TEST_SERVICE_AUTH, running_server

PORT_POSITIVE = 8102
PORT_NEGATIVE = 8103
PLACEHOLDER_TOKEN = "placeholder-token-not-real"
REAL_BASE_URL = "https://morpheus-api.prod.cybersierra.ai/"


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


async def _ask_whoami(port: int, fresh_home: str, *, set_base_url: bool) -> str:
    extra_env = {
        "HOME": fresh_home,
        "CYBERSIERRA_INJECT_ACCESS_TOKEN": "1",
        # Explicitly blank, not omitted, in the negative-control case: the
        # server subprocess's own `load_dotenv(override=False)` would
        # otherwise fall back to whatever's in the real .env file on disk,
        # silently defeating the "unset" simulation this negative control
        # needs. An explicit empty string is present in os.environ, so
        # load_dotenv's override=False leaves it alone.
        "CYBERSIERRA_BASE_URL": REAL_BASE_URL if set_base_url else "",
    }

    with running_server(port=port, extra_env=extra_env) as base_url:
        async with httpx.AsyncClient(
            timeout=90.0, headers={"X-Service-Auth": TEST_SERVICE_AUTH}
        ) as client:
            resp = await client.post(
                f"{base_url}/chat",
                data={
                    "message": "Run exactly this command with the execute tool: "
                    "cybersierra auth whoami. Then tell me exactly what it printed, "
                    "verbatim, including any error message.",
                    "access_token": PLACEHOLDER_TOKEN,
                },
            )
            resp.raise_for_status()
            return _extract_text(resp)


async def main_async() -> int:
    ok = True

    with tempfile.TemporaryDirectory(prefix="verify-fresh-deploy-") as fresh_home:
        # Guard against ever accidentally running this against a real home
        # directory that happens to already have a profile in it.
        assert not (Path(fresh_home) / ".cybersierra").exists()

        print("Positive check: CYBERSIERRA_BASE_URL set, genuinely empty HOME...")
        answer_positive = await _ask_whoami(PORT_POSITIVE, fresh_home, set_base_url=True)
        print(f"  answer: {answer_positive!r}")
        if "No baseUrl configured" in answer_positive:
            print("FAIL: still hit 'No baseUrl configured' with CYBERSIERRA_BASE_URL set — fix did not take effect")
            ok = False
        else:
            print("PASS: no baseUrl error — request reached the real backend without any persisted profile")

    with tempfile.TemporaryDirectory(prefix="verify-fresh-deploy-negctrl-") as fresh_home:
        print("\nNegative control: same fresh HOME, CYBERSIERRA_BASE_URL deliberately unset...")
        answer_negative = await _ask_whoami(PORT_NEGATIVE, fresh_home, set_base_url=False)
        print(f"  answer: {answer_negative!r}")
        if "No baseUrl configured" not in answer_negative:
            print(
                "FAIL: expected the original failure to reproduce without CYBERSIERRA_BASE_URL — "
                "if it didn't, the positive check above isn't actually proving anything"
            )
            ok = False
        else:
            print("PASS: original failure reproduces as expected — the positive check above is meaningful")

    return 0 if ok else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
