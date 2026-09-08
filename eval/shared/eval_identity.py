"""Runtime access-token acquisition for eval runs — uses the dedicated eval
tenant credentials (`EVAL_MORPHEUS_EMAIL`/`PASSWORD`/`ORG`,
`CYBERSIERRA_BASE_URL`, see `.env.example`) to scripted-login via
`cybersierra auth login` (non-interactive — distinct from `login-browser`,
which `harness/sandbox.py` denies the *model* from ever running, but has no
bearing on running it directly here) and read back the resulting JWT, so
every eval query can carry a real per-request `access_token` instead of
falling back to whatever (if anything) is in the machine's default
persisted profile.

Called **once per run** (not per-query — logging in per-query would be
wasteful and risks rate limits) by `eval/local/runner.py` and
`eval/local/gate_runner.py`.

Every login attempt and its result is logged to `logs/eval.log` (see
`eval/shared/observability.py`) — the email/org/url/profile used, the exact
command run (password redacted), and the result (success with the resolved
identity, or failure with the CLI's own stderr). The token value itself is
never logged in full — only a short, non-sensitive prefix, enough to
visibly correlate "this is the token that got attached to requests" in a
demo without printing a usable bearer credential into a log file.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from eval.shared.observability import configure_eval_logging

configure_eval_logging()
logger = logging.getLogger("eval.identity")

CONFIG_PATH = Path.home() / ".cybersierra" / "config.json"
PROFILE = os.environ.get("EVAL_CYBERSIERRA_PROFILE", "eval")

# Used in eval/local/dataset.json's auth_scenarios (the auth-valid-token
# entry) so the file stays valid/readable without a real token committed to
# it — resolved to a real one at runtime by resolve_access_token below, if
# an eval tenant is configured.
PLACEHOLDER_TOKEN = "__PROVIDE_TEST_USER_JWT__"

_ENV_VAR_NAMES = ("EVAL_MORPHEUS_EMAIL", "EVAL_MORPHEUS_PASSWORD", "EVAL_MORPHEUS_ORG", "CYBERSIERRA_BASE_URL")


def _required_env() -> dict[str, str] | None:
    """The four env vars this needs, or None if any are missing — callers
    treat that as "no eval tenant configured here," not a hard error, since
    not every environment has one provisioned."""
    values = {name: os.environ.get(name) for name in _ENV_VAR_NAMES}
    if not all(values.values()):
        return None
    return values


def _token_still_valid(profile_entry: dict) -> bool:
    expires_at = profile_entry.get("expiresAt")
    if not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    # 5-minute safety margin — avoids a token going stale mid-run on a long
    # dataset (same spirit as eval/netra/run_via_production_hop.py's
    # _MorpheusSession, which uses a 60s margin for a much shorter-lived
    # per-request flow).
    return (expiry - datetime.now(timezone.utc)).total_seconds() > 300


def _token_fingerprint(token: str) -> str:
    """Short, non-sensitive prefix for log lines — enough to visibly
    correlate "this is the token in use" across log lines without printing
    a usable bearer credential into a file."""
    return f"{token[:16]}... ({len(token)} chars)"


def get_eval_access_token() -> str | None:
    """A real JWT for the dedicated eval tenant, logging in fresh via
    `cybersierra auth login` (scripted, non-interactive) if none is cached
    under this profile or the cached one is close to expiry. Returns None
    if the eval tenant credentials aren't configured in this environment at
    all — callers should fall back to sending no access_token (today's
    persisted-default-profile behavior) in that case."""
    env = _required_env()
    if env is None:
        logger.info(
            "eval_identity_unavailable reason=%r",
            "EVAL_MORPHEUS_EMAIL/PASSWORD/ORG/CYBERSIERRA_BASE_URL not all set",
            extra={"event": "eval_identity_unavailable"},
        )
        return None

    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text())
        entry = config.get(PROFILE)
        if entry and _token_still_valid(entry):
            logger.info(
                "eval_login_reused profile=%s email=%s tenant=%s expires_at=%s token=%s",
                PROFILE,
                entry.get("email"),
                entry.get("orgId"),
                entry.get("expiresAt"),
                _token_fingerprint(entry["token"]),
                extra={
                    "event": "eval_login_reused",
                    "profile": PROFILE,
                    "email": entry.get("email"),
                    "tenant": entry.get("orgId"),
                    "expires_at": entry.get("expiresAt"),
                },
            )
            return entry["token"]

    command = [
        "cybersierra", "auth", "login",
        "--email", env["EVAL_MORPHEUS_EMAIL"],
        "--password", env["EVAL_MORPHEUS_PASSWORD"],
        "--org", env["EVAL_MORPHEUS_ORG"],
        "--url", env["CYBERSIERRA_BASE_URL"],
        "--profile", PROFILE,
    ]
    redacted_command = [c if c != env["EVAL_MORPHEUS_PASSWORD"] else "***" for c in command]
    logger.info(
        "eval_login_attempt email=%s org=%s url=%s profile=%s command=%s",
        env["EVAL_MORPHEUS_EMAIL"],
        env["EVAL_MORPHEUS_ORG"],
        env["CYBERSIERRA_BASE_URL"],
        PROFILE,
        " ".join(redacted_command),
        extra={
            "event": "eval_login_attempt",
            "email": env["EVAL_MORPHEUS_EMAIL"],
            "org": env["EVAL_MORPHEUS_ORG"],
            "url": env["CYBERSIERRA_BASE_URL"],
            "profile": PROFILE,
            "command": " ".join(redacted_command),
        },
    )

    result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode != 0:
        logger.warning(
            "eval_login_failed profile=%s returncode=%s stderr=%r",
            PROFILE,
            result.returncode,
            result.stderr.strip(),
            extra={"event": "eval_login_failed", "profile": PROFILE, "returncode": result.returncode, "stderr": result.stderr.strip()},
        )
        return None

    if not CONFIG_PATH.exists():
        logger.warning(
            "eval_login_failed profile=%s reason=%r",
            PROFILE,
            f"login reported success but {CONFIG_PATH} still doesn't exist",
            extra={"event": "eval_login_failed", "profile": PROFILE},
        )
        return None

    config = json.loads(CONFIG_PATH.read_text())
    entry = config.get(PROFILE)
    if not entry:
        logger.warning(
            "eval_login_failed profile=%s reason=%r",
            PROFILE,
            f"login reported success but profile {PROFILE!r} not found in {CONFIG_PATH}",
            extra={"event": "eval_login_failed", "profile": PROFILE},
        )
        return None

    logger.info(
        "eval_login_succeeded profile=%s email=%s tenant=%s user_id=%s expires_at=%s token=%s stdout=%r",
        PROFILE,
        entry.get("email"),
        entry.get("orgId"),
        entry.get("userId"),
        entry.get("expiresAt"),
        _token_fingerprint(entry["token"]),
        result.stdout.strip(),
        extra={
            "event": "eval_login_succeeded",
            "profile": PROFILE,
            "email": entry.get("email"),
            "tenant": entry.get("orgId"),
            "user_id": entry.get("userId"),
            "expires_at": entry.get("expiresAt"),
        },
    )
    return entry["token"]


def resolve_access_token(entry_token: str, eval_token: str | None) -> str:
    """Decide what access_token a given query should actually send:
      - an explicit, non-placeholder token already on the entry (e.g.
        auth_scenarios' deliberately-invalid auth-invalid-token case)
        always wins, untouched.
      - the placeholder sentinel gets substituted with a real eval_token if
        one's available — falls back to the placeholder itself (which will
        visibly fail) if not.
      - no token on the entry at all (every other category) uses eval_token
        if available, else stays empty (the pre-existing persisted-profile
        fallback behavior)."""
    if entry_token and entry_token != PLACEHOLDER_TOKEN:
        return entry_token
    if entry_token == PLACEHOLDER_TOKEN:
        return eval_token or entry_token
    return eval_token or ""
