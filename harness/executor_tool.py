"""Deterministic Executor for the cyber-sierra Canonical Execution Plan.

`skills/cyber-sierra/_internal/shared/contracts.md` calls the Executor
"deterministic runtime logic," distinct from the LLM-reasoning components
(Router, Planner, Reflection). We take that literally: this is a plain
Python tool, not a subagent or a prompted step, because the thing it does
(resolve `{{inputs.*}}`/`{{step[N].*}}` references, run one real CLI
command per step, log it, halt on the first non-zero exit code per the
exit-code table in `contracts.md`) is exactly the kind of mechanical,
safety-relevant logic we don't want an LLM improvising — a hallucinated
argument or a skipped halt-on-failure here means a real, possibly
write-operation CLI command runs with the wrong input.

The plan itself (which commands, in what order, with what reasoning) is
still produced by the model, following the Planner/Skill-Resolver
instructions in the ported skill files — this tool only executes an
already-assembled, already-validated `ExecutionPlan`.

The one thing this tool can NOT enforce that the real skill's Orchestration
Protocol step 4 ("Present Plan & Confirm") asks for: an actual runtime
block on `safe: false` steps until a human approves. That gate is,
in this port, still an ordinary conversational instruction to the model
(see harness/agent.py's system prompt) — the same as in the source system,
which also has no hard enforcement of it (Claude Code running the real
skill trusts the model to ask before calling a write command). See
README "Present Plan & Confirm: a considered, not-implemented, gate" for
why we did not wire this to DeepAgents' `interrupt_on` instead.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import time
from datetime import UTC, datetime
from typing import Any

from langchain_core.tools import tool

from deepagents.backends.protocol import SandboxBackendProtocol

# The real CLI invocation for each step is already logged by
# AllowlistedShellBackend.execute (harness/sandbox.py: cli_call_start/
# cli_call_done). This logger adds the Canonical-Execution-Plan-level
# context around it — which step, why (`reason`), which plan — so a log
# reader can tell "the agent ran a command as step 2 of plan X because Y"
# apart from an ad-hoc `execute` call the model made outside any plan.
logger = logging.getLogger("harness.executor")
logger.addHandler(logging.NullHandler())

_INPUT_REF = re.compile(r"^\{\{inputs\.([^}]+)\}\}$")
_STEP_REF = re.compile(r"^\{\{step\[(\d+)\]\.([^}]+)\}\}$")

# skills/cyber-sierra/_internal/shared/contracts.md, "Exit Codes (CLI)".
EXIT_CODE_MEANING = {
    0: "success",
    1: "api_error",
    2: "auth_error",
    3: "not_found",
    4: "forbidden",
}


class PlanExecutionError(ValueError):
    """An ExecutionPlan reference could not be resolved before running its step."""


def _resolve(value: Any, inputs: dict[str, Any], step_outputs: dict[int, Any], current_step_id: int) -> Any:
    """Resolve `{{inputs.*}}` / `{{step[N].*}}` references per
    execution-plan-schema.md's "Reference Syntax" table. Forward references
    (N >= current_step_id) are rejected, matching that schema's own rule.
    """
    if isinstance(value, str):
        if m := _INPUT_REF.match(value):
            key = m.group(1)
            if key not in inputs:
                raise PlanExecutionError(f"unresolved input reference {value!r} — value was never collected")
            return inputs[key]
        if m := _STEP_REF.match(value):
            step_id, path = int(m.group(1)), m.group(2)
            if step_id >= current_step_id:
                raise PlanExecutionError(f"forward reference {value!r} is invalid (N must be < {current_step_id})")
            if step_id not in step_outputs:
                raise PlanExecutionError(f"reference {value!r} points at a step that never ran")
            data = step_outputs[step_id]
            for part in path.split("."):
                if isinstance(data, dict):
                    data = data.get(part)
                elif isinstance(data, list) and part.lstrip("-").isdigit():
                    data = data[int(part)]
                else:
                    raise PlanExecutionError(f"cannot navigate {value!r}: no {part!r} in step {step_id}'s output")
            return data
        return value
    if isinstance(value, dict):
        return {k: _resolve(v, inputs, step_outputs, current_step_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, inputs, step_outputs, current_step_id) for v in value]
    return value


def _build_command(command: str, resolved_args: dict[str, Any]) -> str:
    """`command` is the manifest triple ("module resource action"); flags
    follow manifest-usage.md's "Constructing CLI Commands" table. This is a
    simplification: the CatalogEntry's `params[].in` (path/query/body/file)
    isn't threaded through the ExecutionPlan schema, so every argument
    becomes a plain `--name value` flag except the two names manifest-usage.md
    calls out specially (`data` -> `--data '<json>'`, `file` -> `--file
    <path>`). Good enough for the read-heavy commands this POC plans against;
    a param-location-aware version would need the manifest passed alongside
    the plan, not just the plan itself.
    """
    parts = ["cybersierra", *command.split(), "--format", "json"]
    for name, value in resolved_args.items():
        if name == "data":
            parts += ["--data", shlex.quote(json.dumps(value))]
        elif name == "file":
            parts += ["--file", shlex.quote(str(value))]
        else:
            parts += [f"--{name}", shlex.quote(str(value))]
    return " ".join(parts)


def _split_stderr(output: str) -> tuple[str, str]:
    """LocalShellBackend.execute combines stdout/stderr, prefixing stderr
    lines with `[stderr] ` (see harness/sandbox.py's module docstring for
    where this convention comes from). Undo that so ExecutionLogEntry gets
    real separate stdout/stderr fields, matching execution-plan-schema.md.
    """
    stdout_lines, stderr_lines = [], []
    for line in output.splitlines():
        if line.startswith("[stderr] "):
            stderr_lines.append(line[len("[stderr] ") :])
        else:
            stdout_lines.append(line)
    return "\n".join(stdout_lines), "\n".join(stderr_lines)


def make_run_execution_plan_tool(backend: SandboxBackendProtocol):
    """Bind the Executor tool to one request's sandboxed backend.

    A fresh backend (and so a fresh tool instance) is built per request —
    see harness/agent.py — so this closure never crosses request/token
    boundaries.
    """

    @tool
    def run_execution_plan(plan_json: str, inputs_json: str = "{}") -> str:
        """Execute a Canonical Execution Plan (see
        skills/cyber-sierra/_internal/planner/references/execution-plan-schema.md)
        against the real cybersierra CLI, one step at a time, halting on the
        first step whose exit code is non-zero. Only call this AFTER
        presenting the plan to the user and receiving explicit confirmation
        (Orchestration Protocol step 4) — this tool does not itself ask for
        confirmation or check that you did.

        Args:
            plan_json: JSON-encoded ExecutionPlan (planId, source, sourceName, steps[]).
            inputs_json: JSON-encoded map of every `{{inputs.*}}` value already
                collected from the user. Every input the plan references must
                be present here or the affected step fails before running.

        Returns:
            JSON-encoded `{"result": ExecutionResult, "executionLog": ExecutionLogEntry[]}`.
        """
        plan = json.loads(plan_json)
        inputs = json.loads(inputs_json)
        steps = sorted(plan["steps"], key=lambda s: s["id"])

        logger.info(
            "plan_start planId=%r source=%r steps=%d",
            plan.get("planId"),
            plan.get("source"),
            len(steps),
            extra={"event": "plan_start", "planId": plan.get("planId"), "source": plan.get("source"), "stepCount": len(steps)},
        )

        execution_log: list[dict[str, Any]] = []
        step_outputs: dict[int, Any] = {}
        steps_completed = 0
        steps_failed = 0
        final_output: Any = None

        for step in steps:
            step_id = step["id"]
            timestamp = datetime.now(UTC).isoformat()
            start = time.monotonic()
            logger.info(
                "plan_step_start planId=%r stepId=%d command=%r reason=%r",
                plan.get("planId"),
                step_id,
                step["command"],
                step.get("reason"),
                extra={
                    "event": "plan_step_start",
                    "planId": plan.get("planId"),
                    "stepId": step_id,
                    "command": step["command"],
                    "reason": step.get("reason"),
                },
            )
            try:
                resolved_args = _resolve(step.get("arguments", {}), inputs, step_outputs, step_id)
                resolved_command = _build_command(step["command"], resolved_args)
            except PlanExecutionError as exc:
                logger.warning(
                    "plan_step_failed planId=%r stepId=%d error=%r",
                    plan.get("planId"),
                    step_id,
                    str(exc),
                    extra={"event": "plan_step_failed", "planId": plan.get("planId"), "stepId": step_id, "error": str(exc)},
                )
                execution_log.append(
                    {
                        "stepId": step_id,
                        "command": step["command"],
                        "arguments": step.get("arguments", {}),
                        "stdout": "",
                        "stderr": str(exc),
                        "exitCode": -1,
                        "success": False,
                        "duration": round((time.monotonic() - start) * 1000, 1),
                        "timestamp": timestamp,
                    }
                )
                steps_failed += 1
                break

            # The real CLI invocation itself is logged by
            # AllowlistedShellBackend.execute (cli_call_start/cli_call_done)
            # — this is the same backend the agent's own `execute` tool
            # calls go through, so a plan step and an ad-hoc tool call are
            # both visible in the same cli_call_* log stream.
            response = backend.execute(resolved_command)
            stdout, stderr = _split_stderr(response.output)
            success = response.exit_code == 0
            logger.info(
                "plan_step_done planId=%r stepId=%d exitCode=%d success=%s",
                plan.get("planId"),
                step_id,
                response.exit_code,
                success,
                extra={
                    "event": "plan_step_done",
                    "planId": plan.get("planId"),
                    "stepId": step_id,
                    "exitCode": response.exit_code,
                    "success": success,
                },
            )

            entry = {
                "stepId": step_id,
                "command": resolved_command,
                "arguments": resolved_args,
                "stdout": stdout,
                "stderr": stderr,
                "exitCode": response.exit_code,
                "success": success,
                "duration": round((time.monotonic() - start) * 1000, 1),
                "timestamp": timestamp,
            }
            execution_log.append(entry)

            if success:
                steps_completed += 1
                try:
                    parsed = json.loads(stdout)
                except (json.JSONDecodeError, ValueError):
                    parsed = stdout
                step_outputs[step_id] = parsed
                final_output = parsed
            else:
                steps_failed += 1
                break  # contracts.md: "Halts on first non-zero exit code"

        result = {
            "success": steps_failed == 0 and steps_completed == len(steps),
            "finalOutput": final_output,
            "stepsCompleted": steps_completed,
            "stepsFailed": steps_failed,
        }
        logger.info(
            "plan_done planId=%r success=%s stepsCompleted=%d stepsFailed=%d",
            plan.get("planId"),
            result["success"],
            steps_completed,
            steps_failed,
            extra={
                "event": "plan_done",
                "planId": plan.get("planId"),
                "success": result["success"],
                "stepsCompleted": steps_completed,
                "stepsFailed": steps_failed,
            },
        )
        return json.dumps({"result": result, "executionLog": execution_log})

    return run_execution_plan
