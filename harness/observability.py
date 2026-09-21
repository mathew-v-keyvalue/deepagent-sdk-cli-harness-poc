"""Make the harness's own logging actually visible — to the console AND to
a real log file on disk.

Every module under `harness.*` (`agent.py`, `sandbox.py`, `executor_tool.py`)
logs through `logging.getLogger("harness.<module>")` with a
`logging.NullHandler()` attached directly to it — that's what keeps
`import harness.agent` silent when embedded in something else (e.g. a
verify script) that configures its own logging. `NullHandler` does **not**
stop propagation to the root logger, though, so the one thing actually
needed to see these logs is a handler on the root logger — which neither
`uvicorn server.app:app` nor a bare `python -m harness.agent` sets up on
its own. `configure_logging()` is that one call.

This is the direct answer to "how do I see proof of what's happening under
the hood": every real CLI invocation (`cli_call_start`/`cli_call_done`/
`cli_call_denied`), every tool call the agent makes
(`tool_call_allowed`/`tool_call_denied`), which skill got read
(`skill_loaded`), and each turn's start/end (`turn_start`/`turn_done`) all
go through here — to stderr (so it's visible while the server is running,
same as before) AND to `logs/harness.log` (so it survives after the
terminal/process is gone, which stderr alone never did — this was the gap:
a console handler was the only one ever attached, nothing was ever
persisted to a file).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from harness.console_format import ConsoleStoryFilter, PrettyConsoleFormatter, color_enabled

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_CONFIGURED = False

_DEFAULT_LOG_FILE = PROJECT_ROOT / "logs" / "harness.log"
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB per file
_DEFAULT_BACKUP_COUNT = 5  # harness.log, harness.log.1, ... harness.log.5


def configure_logging() -> None:
    """Idempotent: safe to call from both `server/app.py` (on import) and
    `harness/agent.py`'s `__main__` demo without double-attaching handlers.

    Env vars (all optional):
        HARNESS_LOG_LEVEL — default "INFO".
        HARNESS_LOG_FILE — path to the log file. Default `logs/harness.log`
            (relative to the repo root). Set to an empty string to disable
            file logging entirely and keep only the console handler.
        HARNESS_LOG_MAX_BYTES / HARNESS_LOG_BACKUP_COUNT — rotation policy
            for the file handler. Defaults: 10 MiB, 5 backups (so at most
            ~60 MiB total, not an unbounded file — this logs full CLI
            stdout/stderr previews and tool args on every call, which adds
            up over a long-running server).
        HARNESS_LOG_PRETTY — default "1". The console handler (only —
            `logs/harness.log` is always the full, dense, machine-format
            line) renders as a short colorized story instead — see
            harness/console_format.py. Set to "0" to get the old dense
            format on console too.
        HARNESS_LOG_COLOR — "auto" (default, color iff stderr is a real
            terminal) / "always" / "never". See harness/console_format.py.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level_name = os.environ.get("HARNESS_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    handlers: list[logging.Handler] = []

    console_handler = logging.StreamHandler()
    if os.environ.get("HARNESS_LOG_PRETTY", "1") != "0":
        console_handler.setFormatter(PrettyConsoleFormatter(use_color=color_enabled()))
        console_handler.addFilter(ConsoleStoryFilter())
    else:
        console_handler.setFormatter(formatter)  # old dense format, unchanged escape hatch
    handlers.append(console_handler)

    log_file_setting = os.environ.get("HARNESS_LOG_FILE", str(_DEFAULT_LOG_FILE))
    if log_file_setting:  # empty string explicitly disables file logging
        log_file_path = Path(log_file_setting)
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = int(os.environ.get("HARNESS_LOG_MAX_BYTES", _DEFAULT_MAX_BYTES))
        backup_count = int(os.environ.get("HARNESS_LOG_BACKUP_COUNT", _DEFAULT_BACKUP_COUNT))
        file_handler = logging.handlers.RotatingFileHandler(
            log_file_path, maxBytes=max_bytes, backupCount=backup_count
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    # Attached directly to the "harness" logger (every harness.* module
    # nests under it), not the root logger — so this neither depends on nor
    # interferes with uvicorn's own logging config for "uvicorn"/
    # "uvicorn.access". `propagate = False` stops these records from also
    # reaching whatever (if anything) is already on the root logger, which
    # would otherwise print every line twice.
    harness_logger = logging.getLogger("harness")
    harness_logger.setLevel(level)
    for handler in handlers:
        harness_logger.addHandler(handler)
    harness_logger.propagate = False

    if log_file_setting:
        harness_logger.info(
            "logging_configured level=%s log_file=%s",
            level_name,
            log_file_path,
            extra={"event": "logging_configured", "level": level_name, "log_file": str(log_file_path)},
        )
