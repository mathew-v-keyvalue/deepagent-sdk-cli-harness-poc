"""Shell sandbox: the DeepAgents counterpart to the sibling Claude SDK POC's
``_guard_tool_use`` PreToolUse hook.

Two independent enforcement layers, deliberately redundant:

1. ``AllowlistedShellBackend`` — a ``LocalShellBackend`` subclass that checks
   the command *before* ``subprocess.run`` is ever reached. This is the
   backend that literally owns the syscall, so it cannot be bypassed by any
   gap in a higher-level hook — it is the layer we'd trust even if layer 2
   turned out to be silently shadowed, the way the Claude SDK POC discovered
   ``can_use_tool`` was for Bash in one test environment.
2. ``ShellSandboxMiddleware`` — a ``wrap_tool_call``/``awrap_tool_call``
   middleware, the documented DeepAgents/LangChain mechanism for
   intercepting a tool call before it runs. This is what the Claude POC
   would have used if ``can_use_tool`` had worked as documented. We
   verified (see ``verify/verify_shell_sandbox_denies.py`` and the
   README's "Security boundary" section) that this hook actually fires for
   the ``execute`` tool and prevents the underlying subprocess from ever
   running — unlike the Claude SDK's ``can_use_tool``, which was silently
   never invoked for ``Bash`` in that POC's test environment. We keep both
   layers anyway: layer 1 is what we'd fall back on if a future DeepAgents
   version's middleware-dispatch behavior changed under us the same way.

   One real gap this same direct-instrumentation process caught: this
   harness runs the graph asynchronously (``astream_events``/``ainvoke``),
   and LangChain's async tool-node dispatch does **not** fall back to a
   sync-only ``wrap_tool_call`` — it raises ``NotImplementedError``
   instead (confirmed by running a scripted disallowed tool call through a
   real compiled graph, not just calling the method directly — see
   ``verify/verify_shell_sandbox_denies.py``). That's a loud failure, not
   a silent bypass, but it would still have broken this harness on its
   first tool call had it shipped unnoticed. Both ``wrap_tool_call`` and
   ``awrap_tool_call`` are implemented below because of this.

``deepagents``'s own docs are explicit that ``LocalShellBackend`` ships with
*no* command allowlisting of its own — see its docstring: "Since shell access
is unrestricted... Enable Human-in-the-Loop (HITL) middleware... STRONGLY
RECOMMENDED as your primary safeguard." Neither layer here is HITL (no human
in the loop per call — this is a programmatic allowlist, same shape as the
Claude POC's), so both layers are ours to build; DeepAgents gives us the
hook points, not the policy.

Separately: the real cyber-sierra ``SKILL.md``'s own ``allowed-tools``
frontmatter (``Bash(cybersierra *) ... Read Write``) is *not* an enforcement
mechanism here either — confirmed by reading ``deepagents/middleware/
skills.py`` directly: ``allowed_tools`` parsed from that frontmatter key is
used only to render a descriptive "-> Allowed tools: ..." line in the system
prompt (see ``skills.py`` around the ``lines.append`` call). There is no code
path anywhere in the installed ``deepagents`` package that uses it to gate a
tool call. This is the same lesson the Claude POC learned about SKILL.md's
own ``allowed-tools`` line not being self-enforcing — true for both SDKs,
not a DeepAgents-specific gap.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from deepagents.backends.local_shell import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from harness.tracing import Netra, SpanType, trace_content_enabled

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ToolCallRequest

# "harness.sandbox" nests under the "harness" logger — see
# harness/observability.py for how this is actually made visible when
# running the server, rather than swallowed by logging's default behavior.
logger = logging.getLogger("harness.sandbox")
logger.addHandler(logging.NullHandler())


def scrub(text: str, secret: str | None) -> str:
    """Redact `secret` out of a string before it's logged.

    Used only for log lines, never for the actual `ExecuteResponse`/
    `ToolMessage` handed back to the tool-call machinery — see README
    "Proper logging" for why the real chat response is deliberately left
    unredacted (this harness's own isolation verify script proves per-
    request env scoping by asking the model to echo the injected token
    back, which a blanket scrub would defeat) while anything written to a
    log file gets the secret stripped regardless.
    """
    if not secret:
        return text
    return text.replace(secret, "***REDACTED***")

# Mirrors the real skill's own frontmatter
# (skills/cyber-sierra/SKILL.md: `allowed-tools:`), which is wider than the
# sibling Claude POC's single-binary allowlist: the cybersierra CLI itself,
# `npx cybersierra` (in case it's invoked without a global install), `npm`
# (the install-on-demand step in the skill's own "CLI Environment" section),
# and `python3` (post-processing `cybersierra manifest --raw` JSON, per the
# Orchestration Protocol's Route step). Denied by default; nothing else is
# on this list, including plain `cybersierra` with no install-on-demand
# fallback path outside `npm install -g ...`.
#
# One disclosed, deliberate narrowing beyond that mirror:
# DENIED_COMMAND_PREFIXES below carves the entire `cybersierra auth` group
# back out of the blanket `cybersierra` allow. Not a style choice — see
# that constant's own comment for the measured, log-confirmed harm that
# justified it.
ALLOWED_COMMAND_PREFIXES: tuple[str, ...] = (
    "cybersierra",
    "npx cybersierra",
    "npm",
    "python3",
)

# The entire `cybersierra auth` subcommand group is denied by default, not
# just `login-browser`/`login` (the original two-entry list). Reasons for
# denying the whole group rather than enumerating bad subcommands:
#
# - `login-browser`/`login` are interactive, browser-opening, minutes-long-
#   polling auth-*acquisition* flows that can never succeed in this
#   non-interactive sandbox: confirmed directly in logs/harness.log (a
#   dataset run against a live server) — the model invoked `cybersierra
#   auth login-browser` twice in one turn, burning 120s then a
#   self-escalated 300s before each attempt timed out (exit_code=124), over
#   7 minutes wasted on calls that were always going to fail headlessly.
# - `poll` is the second half of that same interactive flow (resumes a
#   pending browser login session) — same doomed-in-a-headless-sandbox
#   shape.
# - `set-token`/`logout` aren't interactive, but they mutate or delete the
#   one shared, on-disk, process-wide ~/.cybersierra/config.json that every
#   other concurrent/future request on this host depends on — a model
#   invoking either corrupts or destroys every other session's identity,
#   not just its own.
# - Deny-by-default means any *future* `cybersierra auth <new-subcommand>`
#   the real CLI adds is safe by construction, not by remembering to update
#   this list every time.
#
# No production deployment should ever need any of these anyway: a real
# deployment authenticates entirely via the per-request MORPHEUS_TOKEN
# injection (see harness/agent.py's `_build_agent`) plus the unconditional
# MORPHEUS_BASE_URL injection — neither depends on a persisted profile or
# any interactive login, ever, on any host. `cybersierra auth whoami`
# (read-only, no side effects, used successfully throughout logs/
# harness.log as the model's own diagnostic of first resort) is the one
# explicit exception, carved back out of this group deny below.
DENIED_COMMAND_PREFIXES: tuple[str, ...] = ("cybersierra auth",)

# The one deliberate exception to the group deny above.
ALLOWED_DESPITE_DENIED_PREFIXES: tuple[str, ...] = ("cybersierra auth whoami",)

DENY_EXIT_CODE = 126  # POSIX convention: command found but not executable/permitted.


def _matches_prefix(command: str, prefix: str) -> bool:
    return command == prefix or command.startswith(prefix + " ")


def is_command_allowed(command: str) -> bool:
    """True iff ``command`` starts with one of ``ALLOWED_COMMAND_PREFIXES``,
    does not start with one of ``DENIED_COMMAND_PREFIXES``, or is explicitly
    carved back out of a denial via ``ALLOWED_DESPITE_DENIED_PREFIXES``.

    The carve-out is checked first — ``cybersierra auth whoami`` would
    otherwise also match the blanket ``cybersierra auth`` denial.

    Prefix matching only — like the sibling Claude POC's
    ``_guard_tool_use``, this does not parse shell grammar. A command like
    ``cybersierra manifest; rm -rf /`` still starts with an allowed prefix
    and is NOT caught by this check; the semicolon-joined second command
    runs too, because ``LocalShellBackend.execute`` (like the Claude SDK's
    Bash tool) invokes the whole string via a real shell. See README "Known
    limitations" — this is the same class of gap in both POCs, not
    something DeepAgents introduces.
    """
    command = command.strip()
    if not command:
        return False
    if any(_matches_prefix(command, prefix) for prefix in ALLOWED_DESPITE_DENIED_PREFIXES):
        return True
    if any(_matches_prefix(command, prefix) for prefix in DENIED_COMMAND_PREFIXES):
        return False
    return any(_matches_prefix(command, prefix) for prefix in ALLOWED_COMMAND_PREFIXES)


def _denial_message(command: str) -> str:
    command = command.strip()
    if any(_matches_prefix(command, prefix) for prefix in DENIED_COMMAND_PREFIXES):
        return (
            f"Error: command not permitted in this sandbox: {command!r}. "
            "Authentication for this session is already handled automatically -- "
            "do not attempt to log in, re-authenticate, or modify stored credentials "
            "yourself. If a command fails with an auth-shaped error, that reflects a "
            "real access issue to report to the user in plain language, not a missing "
            "login step to fix. `cybersierra auth whoami` (read-only) is still available."
        )
    return (
        f"Error: command not permitted in this sandbox: {command!r}. "
        f"Only commands starting with one of {ALLOWED_COMMAND_PREFIXES!r} may run here."
    )


# Modes where write-shaped tool calls are hard-denied at this layer, rather
# than reaching agent_auto's interrupt_on pause (harness/agent.py) -- see
# poc-wiki/execution-modes/mode-design.md. "ask" and "agent_plan" both stop
# here; only "agent_auto" ever lets a write-shaped call through this
# middleware unconditionally allowed.
READ_ONLY_MODES: tuple[str, ...] = ("ask", "agent_plan")


def _plan_has_unsafe_step(args: dict[str, Any]) -> bool:
    """True if a `run_execution_plan` call's `plan_json` argument contains
    any step with `"safe": false` (execution-plan-schema.md's own read/write
    signal), or if `plan_json` can't be parsed as a plan at all --
    unparseable input fails safe (treated as unsafe) rather than silently
    passing through a read-only mode's hard gate.
    """
    try:
        plan = json.loads(str(args.get("plan_json", "")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return True
    steps = plan.get("steps", []) if isinstance(plan, dict) else []
    return any(isinstance(step, dict) and step.get("safe") is False for step in steps)


# Confirmed live (2026-09-14, real server, real tenant): the model routinely
# writes via this ad-hoc `execute` path instead of `run_execution_plan` --
# two real records got created in a live tenant with zero approval before
# this existed, because `interrupt_on` only ever covered `run_execution_plan`.
# `execute` has no `safe` field of its own the way a plan step does, so
# classifying it means matching the raw command against the real manifest's
# own `module resource action` -> `safe` mapping instead.
_MANIFEST_OPERATION = re.compile(r"^(?:npx\s+)?cybersierra\s+([a-z][\w-]*)\s+([a-z][\w-]*)\s+([a-z][\w-]*)\b")

_manifest_tree_cache: dict[str, list[dict[str, Any]]] | None = None


def _get_manifest_tree() -> dict[str, list[dict[str, Any]]]:
    """Process-lifetime cache of `cybersierra manifest --raw`'s module tree --
    tied to CLI version, not to any user/session, so caching it for the life
    of the process is safe (same "no expiry" reasoning already settled for
    sessions elsewhere in this design). Deliberately synchronous/blocking:
    `interrupt_on`'s `when` predicate (harness/agent.py) is itself sync, and
    this only ever actually shells out once per process -- every call after
    the first hits the cache.
    """
    global _manifest_tree_cache
    if _manifest_tree_cache is None:
        try:
            result = subprocess.run(
                ["cybersierra", "manifest", "--raw"], capture_output=True, text=True, timeout=30, check=False
            )
            _manifest_tree_cache = json.loads(result.stdout)["tree"] if result.returncode == 0 else {}
        except Exception:
            logger.exception(
                "manifest_fetch_failed -- ad-hoc execute commands will fail safe (treated as unsafe) until this succeeds"
            )
            _manifest_tree_cache = {}
    return _manifest_tree_cache


def _is_execute_command_unsafe(command: str) -> bool:
    """True if an ad-hoc `execute` call's raw command string is write-shaped
    -- the `execute`-side counterpart to `_plan_has_unsafe_step` (which does
    the same job for a structured plan step's own `safe` field). Classifies
    by matching the command against the real manifest's `module resource
    action` -> `safe` mapping (fetched once, cached -- see
    `_get_manifest_tree`).

    Fails safe (returns `True`, "needs approval") whenever classification
    can't be made confidently: the manifest lookup itself failed, the
    operation isn't found in it, or the manifest doesn't mark it `"safe":
    true` explicitly -- mirrors `_plan_has_unsafe_step`'s own "anything not
    explicitly safe" rule. Returns `False` for anything that isn't a
    `cybersierra <module> <resource> <action>`-shaped command at all (e.g.
    `cybersierra --version`, `cybersierra manifest --raw`, `npm list -g
    ...`) -- these are CLI/environment housekeeping, not tenant-data
    operations, so there's nothing here to gate. Also `False` for a
    `--help` lookup on an otherwise write-shaped operation (e.g.
    `cybersierra tprm assessees create --help`) -- confirmed live this
    otherwise pauses on a request for documentation text, never touching
    tenant data at all; asking a human to approve reading help output is
    pure friction with no safety benefit.
    """
    command = command.strip()
    if re.search(r"(?:^|\s)--help\b", command) or command.endswith(" help"):
        return False
    match = _MANIFEST_OPERATION.match(command)
    if not match:
        return False
    module, resource, action = match.groups()
    tree = _get_manifest_tree()
    for op in tree.get(module, []):
        if op.get("resource") == resource and op.get("action") == action:
            return op.get("safe") is not True
    return True


class AllowlistedShellBackend(LocalShellBackend):
    """``LocalShellBackend`` with a deny-by-default command allowlist.

    This is enforcement layer 1 (see module docstring) — the check runs
    inside the same method that would otherwise call ``subprocess.run``,
    so there is no code path from the ``execute`` tool to a real subprocess
    that skips it. Also the one place that logs every real CLI invocation
    this harness makes — "cli_call_start" before the subprocess runs,
    "cli_call_done"/"cli_call_denied" after — which is the actual proof, in
    a log line, of what command ran and what it returned. See README
    "Proper logging".
    """

    def __init__(self, *args: Any, redact: str | None = None, **kwargs: Any) -> None:
        """`redact`, if given, is scrubbed out of this backend's own log
        lines only (never out of the real `ExecuteResponse` returned to the
        caller) — pass the current request's `access_token` here so it
        never ends up in a log file even if a command happens to print it
        (e.g. `python3 -c "...os.environ.get('MORPHEUS_TOKEN')..."`,
        which is exactly what verify/verify_server_multi_session_isolation.py
        deliberately asks the model to run).
        """
        super().__init__(*args, **kwargs)
        self._redact = redact

    @property
    def redact_secret(self) -> str | None:
        """The current request's access token, if any — exposed so other
        modules sharing this backend (harness/executor_tool.py's `Plan_Step`
        span) can scrub the same secret out of anything they attach to a
        Netra span, the same way this backend already scrubs it from its
        own `cli_call_*` log lines. Never used to alter what's returned to
        the tool-call machinery, only what's exported externally."""
        return self._redact

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        with Netra.start_span("cli_call", as_type=SpanType.TOOL, module_name="sandbox") as span:
            span.set_attribute("cli.command", scrub(command, self._redact))
            # Generic input/output, alongside the cli.* attributes above/below —
            # SpanWrapper has no set_input()/set_output() (only Netra.set_root_input/
            # output exist, and those only ever target the trace's root span), so this
            # is the only way this hand-created span's content shows up under the
            # dashboard's standard input/output fields rather than only as cli.*.
            span.set_attribute("input", scrub(command, self._redact))

            if not is_command_allowed(command):
                logger.warning(
                    "cli_call_denied command=%r",
                    scrub(command, self._redact),
                    extra={"event": "cli_call_denied", "command": scrub(command, self._redact)},
                )
                span.set_attribute("cli.status", "denied")
                span.set_attribute("output", scrub(_denial_message(command), self._redact))
                span.set_success()
                return ExecuteResponse(
                    output=_denial_message(command),
                    exit_code=DENY_EXIT_CODE,
                    truncated=False,
                )

            logger.info(
                "cli_call_start command=%r",
                scrub(command, self._redact),
                extra={"event": "cli_call_start", "command": scrub(command, self._redact)},
            )
            start = time.monotonic()
            try:
                response = super().execute(command, timeout=timeout)
            except Exception as exc:
                span.set_attribute("cli.status", "error")
                span.set_error(str(exc))
                raise
            duration_ms = round((time.monotonic() - start) * 1000)
            logger.info(
                "cli_call_done command=%r exit_code=%s truncated=%s output=%r",
                scrub(command, self._redact),
                response.exit_code,
                response.truncated,
                scrub(response.output, self._redact)[:500],
                extra={
                    "event": "cli_call_done",
                    "command": scrub(command, self._redact),
                    "exit_code": response.exit_code,
                    "truncated": response.truncated,
                    "output_preview": scrub(response.output, self._redact)[:500],
                    "duration_ms": duration_ms,
                },
            )
            if trace_content_enabled():
                span.set_attribute("cli.output", scrub(response.output, self._redact)[:2000])
                span.set_attribute("output", scrub(response.output, self._redact)[:2000])
            span.set_attribute("cli.exit_code", str(response.exit_code))
            span.set_attribute("cli.truncated", str(response.truncated))
            span.set_attribute("cli.status", "ok" if response.exit_code == 0 else "nonzero_exit")
            span.set_success()
            return response


class ShellSandboxMiddleware(AgentMiddleware):
    """Enforcement layer 2 (see module docstring): denies at the tool-call
    layer, before the ``execute`` tool (and therefore the backend) is ever
    invoked.

    Also denies any tool call that isn't ``execute``, ``run_execution_plan``,
    ``task``, or one of the filesystem tools DeepAgents wires up by default —
    belt-and-braces against a future middleware/tool addition this harness
    didn't anticipate. In this POC's actual tool set (filesystem tools +
    execute + run_execution_plan + task, see ``harness/agent.py``) this
    second check is a no-op; it exists so that adding a new tool without
    updating this allowlist fails closed, not open.

    ``run_execution_plan`` reaching this middleware's known-safe path at all
    was a separate, pre-existing bug fix (see git history) — it was
    previously reachable by neither the ``execute`` branch nor
    ``_KNOWN_SAFE_TOOLS``, so every call to it was denied outright, in every
    mode, unconditionally.

    ``mode`` (one of ``"ask"``, ``"agent_plan"``, ``"agent_auto"`` — see
    ``poc-wiki/execution-modes/mode-design.md``) hard-denies write-shaped
    tool calls (``execute``, ``run_execution_plan`` with an unsafe step,
    ``write_file``/``edit_file``/``delete``) in ``READ_ONLY_MODES``. This is
    a separate, independent mechanism from ``agent_auto``'s one-time
    ``interrupt_on`` pause (``harness/agent.py``): this layer either denies
    a call outright or lets it through unchanged; it never pauses one
    itself. One exception: a write targeting ``skills/_generated/`` (the
    model persisting a self-extension skill) is registered in
    ``agent_plan``'s ``interrupt_on`` too (see
    ``harness/agent.py``'s ``_build_interrupt_on``), so it never reaches
    this middleware's hard-deny in the first place until it's already been
    approved — this layer lets that one case through even in
    ``READ_ONLY_MODES`` (see ``poc-wiki/execution-modes/decisions-log.md``'s
    "Filesystem writes / skill self-extension" section).
    """

    name = "ShellSandboxMiddleware"

    _KNOWN_SAFE_TOOLS = frozenset({"ls", "read_file", "glob", "grep", "task"})

    _WRITE_SHAPED_FS_TOOLS = frozenset({"write_file", "edit_file", "delete"})

    def __init__(self, *, redact: str | None = None, mode: str = "agent_auto") -> None:
        """`redact`, if given, is scrubbed from this middleware's own log
        lines only — see `AllowlistedShellBackend.__init__` for why. Pass
        the current request's `access_token`. `mode` gates write-shaped
        tool calls per `READ_ONLY_MODES` above."""
        super().__init__()
        self._redact = redact
        self._mode = mode

    @staticmethod
    def _is_skill_read(name: str | None, args: dict[str, Any]) -> str | None:
        """If this is a `read_file` call on a `SKILL.md`, return the skill's
        directory name (e.g. `"cyber-sierra"` for `.../skills/cyber-sierra/
        SKILL.md`) so it can be logged as a distinct `skill_loaded` event —
        the direct answer to "what skill got loaded," as opposed to the
        generic `tool_call` log every `read_file` call also gets. Returns
        `None` for anything else, including a `read_file` on some other
        file.
        """
        if name != "read_file":
            return None
        path = str(args.get("file_path", ""))
        if not path.endswith("SKILL.md"):
            return None
        parts = PurePosixPath(path.replace("\\", "/")).parts
        return parts[-2] if len(parts) >= 2 else path

    @staticmethod
    def _is_skill_write(name: str | None, args: dict[str, Any]) -> bool:
        """True if this is a `write_file`/`edit_file`/`delete` call whose
        path targets `skills/_generated/` — i.e. the model persisting a
        self-extension skill (R4/"Reflection"), not an ordinary filesystem
        write. Write-side mirror of `_is_skill_read`'s path inspection;
        returns a plain bool (not a skill name) since the mode-aware
        decision below only needs yes/no.
        """
        if name not in ShellSandboxMiddleware._WRITE_SHAPED_FS_TOOLS:
            return False
        path = str(args.get("file_path", "")).replace("\\", "/")
        return "skills/_generated/" in path

    def _decide(self, request: "ToolCallRequest") -> ToolMessage | None:
        """Shared sync/async decision logic. Returns a denial `ToolMessage`
        to short-circuit, or `None` to mean "call the handler". Logs every
        decision either way — this is the tool-call-level half of "what
        happened under the hood" (the CLI-specific half is
        `AllowlistedShellBackend`'s `cli_call_*` logging)."""
        tool_call = request.tool_call
        name = tool_call.get("name")
        args = tool_call.get("args") or {}

        skill = self._is_skill_read(name, args)
        if skill:
            logger.info(
                "skill_loaded name=%r path=%r",
                skill,
                args.get("file_path"),
                extra={"event": "skill_loaded", "skill": skill, "path": args.get("file_path")},
            )

        if name == "execute":
            command = str(args.get("command", ""))
            if self._mode in READ_ONLY_MODES:
                logger.warning(
                    "tool_call_denied name=execute mode=%r command=%r (ad-hoc shell unavailable in read-only mode)",
                    self._mode,
                    scrub(command, self._redact),
                    extra={
                        "event": "tool_call_denied",
                        "tool_name": name,
                        "mode": self._mode,
                        "command": scrub(command, self._redact),
                    },
                )
                return ToolMessage(
                    content=(
                        f"Error: ad-hoc shell commands aren't available in {self._mode!r} mode. "
                        "Use run_execution_plan with read-only steps for lookups instead."
                    ),
                    name=name,
                    tool_call_id=tool_call["id"],
                    status="error",
                )
            if not is_command_allowed(command):
                logger.warning(
                    "tool_call_denied name=execute command=%r",
                    scrub(command, self._redact),
                    extra={"event": "tool_call_denied", "tool_name": name, "command": scrub(command, self._redact)},
                )
                return ToolMessage(
                    content=_denial_message(command),
                    name=name,
                    tool_call_id=tool_call["id"],
                    status="error",
                )
            logger.info(
                "tool_call_allowed name=execute command=%r",
                scrub(command, self._redact),
                extra={"event": "tool_call_allowed", "tool_name": name, "command": scrub(command, self._redact)},
            )
            return None

        if name == "run_execution_plan":
            if self._mode in READ_ONLY_MODES and _plan_has_unsafe_step(args):
                logger.warning(
                    "tool_call_denied name=run_execution_plan mode=%r (unsafe step in read-only mode)",
                    self._mode,
                    extra={"event": "tool_call_denied", "tool_name": name, "mode": self._mode},
                )
                return ToolMessage(
                    content=(
                        f"Error: this plan contains a write step (\"safe\": false), which isn't "
                        f"available in {self._mode!r} mode. Present the plan to the user and ask "
                        "them to switch to Agent Auto mode before it can run."
                    ),
                    name=name,
                    tool_call_id=tool_call["id"],
                    status="error",
                )
            logger.info(
                "tool_call_allowed name=run_execution_plan mode=%r",
                self._mode,
                extra={"event": "tool_call_allowed", "tool_name": name, "mode": self._mode},
            )
            return None

        if name in self._WRITE_SHAPED_FS_TOOLS:
            is_skill_write = self._is_skill_write(name, args)
            if self._mode in READ_ONLY_MODES and not (self._mode == "agent_plan" and is_skill_write):
                # agent_plan's skill-write exception: a skill-write in this
                # mode is registered in interrupt_on (see
                # harness/agent.py's _build_interrupt_on), so this middleware
                # only ever sees one of two cases -- an ordinary write
                # (never reaches interrupt_on, denied here unconditionally,
                # same as `ask`) or a skill-write that already paused for
                # approval and is now resuming after an "approve" decision
                # (interrupt_on would never have let a "reject" reach this
                # point at all). Denying it here too would make an approved
                # agent_plan skill-write silently no-op -- confirmed live as
                # a real bug before this fix (see git history).
                logger.warning(
                    "tool_call_denied name=%r mode=%r is_skill_write=%s (filesystem write unavailable in read-only mode)",
                    name,
                    self._mode,
                    is_skill_write,
                    extra={
                        "event": "tool_call_denied",
                        "tool_name": name,
                        "mode": self._mode,
                        "is_skill_write": is_skill_write,
                    },
                )
                return ToolMessage(
                    content=f"Error: filesystem writes aren't available in {self._mode!r} mode.",
                    name=name,
                    tool_call_id=tool_call["id"],
                    status="error",
                )
            logger.info(
                "tool_call_allowed name=%r mode=%r",
                name,
                self._mode,
                extra={"event": "tool_call_allowed", "tool_name": name, "mode": self._mode},
            )
            return None

        if name in self._KNOWN_SAFE_TOOLS:
            logger.info(
                "tool_call_allowed name=%r args=%r",
                name,
                scrub(str(args), self._redact)[:300],
                extra={"event": "tool_call_allowed", "tool_name": name, "args_preview": scrub(str(args), self._redact)[:300]},
            )
            return None

        logger.warning(
            "tool_call_denied name=%r (unrecognized tool)",
            name,
            extra={"event": "tool_call_denied", "tool_name": name},
        )
        return ToolMessage(
            content=f"Error: tool {name!r} is not permitted in this harness.",
            name=name or "unknown",
            tool_call_id=tool_call["id"],
            status="error",
        )

    def wrap_tool_call(self, request: "ToolCallRequest", handler: Any) -> Any:
        denial = self._decide(request)
        return denial if denial is not None else handler(request)

    async def awrap_tool_call(self, request: "ToolCallRequest", handler: Any) -> Any:
        denial = self._decide(request)
        return denial if denial is not None else await handler(request)
