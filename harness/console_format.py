"""Story-shaped console logging — the human-facing counterpart to
`harness/observability.py`'s plain, machine-parseable file log.

The file handler (`logs/harness.log`) keeps its exact current format
untouched: same `logging.Formatter`, no filter. Everything here is wired
onto the CONSOLE handler only (see `configure_logging()`), so a live demo
tail reads as a short, colorized story — request in, model reasoning, a
command entering the sandbox, the real CLI running, done — while the file
still has full, dense detail for later debugging.

Every `harness.*` log call already tags itself with `extra={"event": "...",
...}` (see harness/agent.py, harness/sandbox.py, harness/executor_tool.py).
`_EVENT_TABLE` below is the single place that decides, per event name,
whether it's part of the story at all and if so what one line of it reads.
An event this table doesn't know about is never silently dropped — it
renders generically instead, so a future new event type is visible by
default rather than invisible by omission.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Callable

try:
    import colorama

    colorama.just_fix_windows_console()  # no-op off Windows; safe if this ever isn't installed
except Exception:  # noqa: BLE001 — color support must never block logging
    pass

_ANSI = {
    "cyan": "\033[36m",
    "magenta": "\033[35m",
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}

_LINE_LIMIT = 100  # keeps one story beat to one terminal line


def color_enabled() -> bool:
    """`HARNESS_LOG_COLOR=always|never` overrides everything; otherwise the
    informal `NO_COLOR` convention is honored; otherwise color follows
    whether stderr (where the console handler writes) is an actual
    terminal — so redirecting output to a file never leaks raw ANSI codes.
    """
    mode = os.environ.get("HARNESS_LOG_COLOR", "auto").lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    if "NO_COLOR" in os.environ:
        return False
    return sys.stderr.isatty()


def _one_line(text: Any, limit: int = _LINE_LIMIT) -> str:
    """Collapse embedded newlines/tabs/runs of whitespace (e.g. a multi-line
    `python3 -c "..."` command, or a multi-line error) and truncate — every
    builder below runs free-form fields through this so the "one line per
    story beat" format can't break.
    """
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _build_turn_start(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    tracker.counts = {"llm": 0, "cli": 0}
    prompt = getattr(record, "prompt_preview", "")
    return f'"{_one_line(prompt, 80)}"'


def _build_llm_call_start(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    return "reasoning..."


def _build_llm_call_end(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    tracker.counts["llm"] += 1
    elapsed_ms = getattr(record, "elapsed_ms", None)
    return f"reasoning... ({elapsed_ms}ms)" if elapsed_ms is not None else "reasoning..."


def _build_skill_loaded(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    return f"{getattr(record, 'skill', '?')}/SKILL.md loaded"


def _build_skill_load_error(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    return _one_line(getattr(record, "error", "unknown error"))


def _build_tool_call_allowed(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    # Fires for every allowed tool (read_file, write_file, ls, ...), not
    # just real CLI commands — collapsing it to only the `execute` case is
    # what turns this from the noisiest event into the "entering the
    # sandbox" story beat.
    if getattr(record, "tool_name", None) != "execute":
        return None
    command = getattr(record, "command", "") or getattr(record, "args_preview", "")
    return f"{_one_line(command)} → allowed"


def _build_tool_call_denied(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    target = getattr(record, "command", None) or getattr(record, "tool_name", "?")
    return f"{_one_line(target)} → DENIED"


def _build_cli_call_done(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    tracker.counts["cli"] += 1
    command = _one_line(getattr(record, "command", "?"))
    exit_code = getattr(record, "exit_code", "?")
    duration_ms = getattr(record, "duration_ms", None)
    suffix = f" ({duration_ms}ms)" if duration_ms is not None else ""
    tracker.last_cli_ok = exit_code == 0
    return f"{command} → exit {exit_code}{suffix}"


def _build_plan_start(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    return f"{getattr(record, 'stepCount', '?')} step(s)"


def _build_plan_step_failed(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    return f"step {getattr(record, 'stepId', '?')} failed: {_one_line(getattr(record, 'error', ''), 80)}"


def _build_plan_done(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    if getattr(record, "success", True):
        return None  # redundant with the closing DONE line
    completed = getattr(record, "stepsCompleted", "?")
    failed = getattr(record, "stepsFailed", "?")
    return f"failed ({completed} completed, {failed} failed)"


def _build_turn_done(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    return f"{tracker.counts.get('llm', 0)} LLM call(s), {tracker.counts.get('cli', 0)} CLI call(s)"


def _build_harness_error(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    return _one_line(record.getMessage(), 120)


def _build_logging_configured(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    return f"level={getattr(record, 'level', '?')} file={getattr(record, 'log_file', '?')}"


def _build_suppressed(record: logging.LogRecord, tracker: "ConsoleStoryFilter") -> str | None:
    return None


# name -> (icon, label, color, builder). `color` may be a plain color name
# (looked up in `_ANSI`) or a callable `record -> color_name` for
# status-dependent coloring (e.g. green/red by exit code).
_EVENT_TABLE: dict[str, tuple[str, str, Any, Callable[[logging.LogRecord, "ConsoleStoryFilter"], str | None]]] = {
    "turn_start": ("▶", "REQUEST", "cyan", _build_turn_start),
    "llm_call_start": ("🧠", "LLM", "magenta", _build_llm_call_start),
    "llm_call_end": ("🧠", "LLM", "magenta", _build_llm_call_end),
    "skills_available": ("", "", "dim", _build_suppressed),
    "skill_load_error": ("⚠", "SKILL", "yellow", _build_skill_load_error),
    "skill_loaded": ("📖", "SKILL", "yellow", _build_skill_loaded),
    "tool_call_allowed": ("🔐", "SANDBOX", "blue", _build_tool_call_allowed),
    "tool_call_denied": ("🔐", "SANDBOX", "red", _build_tool_call_denied),
    "cli_call_start": ("", "", "dim", _build_suppressed),
    "cli_call_done": ("💻", "CLI", lambda r: "green" if getattr(r, "exit_code", 1) == 0 else "red", _build_cli_call_done),
    "cli_call_denied": ("", "", "dim", _build_suppressed),
    "plan_start": ("📋", "PLAN", "cyan", _build_plan_start),
    "plan_step_start": ("", "", "dim", _build_suppressed),
    "plan_step_done": ("", "", "dim", _build_suppressed),
    "plan_step_failed": ("📋", "PLAN", "red", _build_plan_step_failed),
    "plan_done": ("📋", "PLAN", "red", _build_plan_done),
    "turn_done": ("✅", "DONE", "green", _build_turn_done),
    "harness_error": ("❌", "ERROR", "red", _build_harness_error),
    "logging_configured": ("⚙", "START", "dim", _build_logging_configured),
}


class ConsoleStoryFilter(logging.Filter):
    """Decides whether a record is part of the console story and, if so,
    stashes the rendered text/icon/label/color onto the record for
    `PrettyConsoleFormatter` to lay out. Never raises: a bug here must fail
    open (show the raw line) rather than crash logging or hide output
    mid-demo.
    """

    def __init__(self) -> None:
        super().__init__()
        self.counts = {"llm": 0, "cli": 0}
        self.last_cli_ok = True

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            event = getattr(record, "event", None)
            entry = _EVENT_TABLE.get(event)
            if entry is None:
                # Unmapped event: never silently invisible — render
                # generically so a future new event type is still seen.
                record.pretty_icon = "•"
                record.pretty_label = record.levelname
                record.pretty_color = "dim"
                record.pretty_text = _one_line(record.getMessage(), 120)
                return True

            icon, label, color, builder = entry
            text = builder(record, self)
            if text is None:
                return False
            record.pretty_icon = icon
            record.pretty_label = label
            record.pretty_color = color(record) if callable(color) else color
            record.pretty_text = text
            return True
        except Exception:  # noqa: BLE001 — fail open, see docstring
            record.pretty_icon = "•"
            record.pretty_label = record.levelname
            record.pretty_color = "dim"
            record.pretty_text = _one_line(record.getMessage(), 120)
            return True


class PrettyConsoleFormatter(logging.Formatter):
    """Lays out `HH:MM:SS  <icon> <label>  <text>`, colorizing icon+label
    when enabled. Deliberately does not call `logging.Formatter.format()`
    (which would append a full traceback via `exc_text`) — an error gets one
    short line on console; the file handler still gets the full traceback
    via the untouched default formatter on the same log record.
    """

    def __init__(self, *, use_color: bool) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, self.datefmt)
        icon = getattr(record, "pretty_icon", "•")
        label = getattr(record, "pretty_label", record.levelname)
        text = getattr(record, "pretty_text", record.getMessage())
        color_name = getattr(record, "pretty_color", None)

        tag = f"{icon} {label:<8}".rstrip()
        if self._use_color and color_name and color_name in _ANSI:
            tag = f"{_ANSI[color_name]}{tag}{_ANSI['reset']}"
        return f"{timestamp}  {tag}  {text}"
