"""Make the eval framework's own logging actually visible — to the console
AND to a real log file on disk, separate from `logs/harness.log`.

Mirrors `harness/observability.py`'s pattern exactly, on purpose: same
rotation policy, same line format, same idempotent-configure shape — just a
different logger namespace (`"eval"`, not `"harness"`) and a different file
(`logs/eval.log`, not `logs/harness.log`). Kept as a separate file rather
than writing into `logs/harness.log` because these are two different
processes (the eval CLI vs. the running `uvicorn server.app:app`) — sharing
one log file across two independent processes means fighting over the same
file handle for no real benefit, and blurs two genuinely different
concerns: server request-handling vs. eval-run orchestration (auth
acquisition, which query ran with which identity, pass/fail).

This is the durable answer to "prove exactly what happened during this eval
run, including the auth flow" — every `eval/shared/eval_identity.py` login
attempt/result and every `eval/local/runner.py`/`gate_runner.py` query's
identity mode goes through here, so it survives after the terminal is gone.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_CONFIGURED = False

_DEFAULT_LOG_FILE = PROJECT_ROOT / "logs" / "eval.log"
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB per file
_DEFAULT_BACKUP_COUNT = 5


def configure_eval_logging() -> None:
    """Idempotent — safe to call from every eval entry point
    (`eval/local/runner.py`, `gate_runner.py`, `eval/shared/eval_identity.py`)
    without double-attaching handlers.

    Env vars (all optional, same shape as harness/observability.py's):
        EVAL_LOG_LEVEL — default "INFO".
        EVAL_LOG_FILE — path to the log file. Default `logs/eval.log`
            (relative to the repo root). Empty string disables file logging.
        EVAL_LOG_MAX_BYTES / EVAL_LOG_BACKUP_COUNT — rotation policy.
            Defaults: 10 MiB, 5 backups.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level_name = os.environ.get("EVAL_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    handlers[0].setFormatter(formatter)

    log_file_setting = os.environ.get("EVAL_LOG_FILE", str(_DEFAULT_LOG_FILE))
    log_file_path = None
    if log_file_setting:
        log_file_path = Path(log_file_setting)
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = int(os.environ.get("EVAL_LOG_MAX_BYTES", _DEFAULT_MAX_BYTES))
        backup_count = int(os.environ.get("EVAL_LOG_BACKUP_COUNT", _DEFAULT_BACKUP_COUNT))
        file_handler = logging.handlers.RotatingFileHandler(log_file_path, maxBytes=max_bytes, backupCount=backup_count)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    eval_logger = logging.getLogger("eval")
    eval_logger.setLevel(level)
    for handler in handlers:
        eval_logger.addHandler(handler)
    eval_logger.propagate = False

    if log_file_path:
        eval_logger.info(
            "logging_configured level=%s log_file=%s",
            level_name,
            log_file_path,
            extra={"event": "logging_configured", "level": level_name, "log_file": str(log_file_path)},
        )
