"""Make the harness's own logging actually visible.

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
go through here.
"""

from __future__ import annotations

import logging
import os

_CONFIGURED = False


def configure_logging() -> None:
    """Idempotent: safe to call from both `server/app.py` (on import) and
    `harness/agent.py`'s `__main__` demo without double-attaching handlers.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level_name = os.environ.get("HARNESS_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s"))

    # Attached directly to the "harness" logger (every harness.* module
    # nests under it), not the root logger — so this neither depends on nor
    # interferes with uvicorn's own logging config for "uvicorn"/
    # "uvicorn.access". `propagate = False` stops these records from also
    # reaching whatever (if anything) is already on the root logger, which
    # would otherwise print every line twice.
    harness_logger = logging.getLogger("harness")
    harness_logger.setLevel(level)
    harness_logger.addHandler(handler)
    harness_logger.propagate = False
