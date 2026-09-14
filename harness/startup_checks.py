"""Startup compatibility checks for the execution-modes primitives
`harness/agent.py`'s mode gating depends on.

Mirrors `eval/netra/run.py`'s `_assert_netra_internals_compatible()`
exactly: pin the exact installed versions this design was verified
against, `hasattr`/import-check the specific internals depended on, and
fail loudly (`sys.exit`) at process start rather than confusingly on a
live request. A silent version drift here would otherwise surface as
`interrupt_on` behaving differently, or the middleware-composition order
`harness/sandbox.py`/`harness/agent.py` both assume quietly changing —
see `poc-wiki/execution-modes/mode-design.md`'s "Primitives available"
section and `architecture-changes.md` for what was actually verified.

Call `assert_mode_dependencies_compatible()` once, at import time, before
the server starts accepting requests — see `server/app.py`.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import sys

# Pinned deliberately: harness/agent.py's interrupt_on/TodoListMiddleware
# wiring was verified against exactly these installed versions. A version
# bump doesn't necessarily break anything -- it just means nobody has
# re-verified interrupt_on's semantics, the HITLRequest/Decision shapes, or
# the middleware-composition order against the new release yet.
_EXPECTED_VERSIONS = {
    "deepagents": "0.7.13",
    "langchain": "1.3.18",
    "langgraph": "1.2.11",
}


def assert_mode_dependencies_compatible() -> None:
    """Fail loudly at process start if the installed deepagents/langchain/
    langgraph versions, or the specific internals `_build_agent` depends
    on for mode gating, don't match what was verified during design.
    """
    mismatched = {
        pkg: (expected, importlib.metadata.version(pkg))
        for pkg, expected in _EXPECTED_VERSIONS.items()
        if importlib.metadata.version(pkg) != expected
    }
    if mismatched:
        details = ", ".join(
            f"{pkg} expected=={exp} installed=={got}" for pkg, (exp, got) in mismatched.items()
        )
        sys.exit(
            f"Version mismatch for a dependency this harness's mode gating was verified against: "
            f"{details}. Re-verify interrupt_on/HumanInTheLoopMiddleware/TodoListMiddleware behavior "
            "and middleware-composition order against the new version before running this server -- "
            "see poc-wiki/execution-modes/mode-design.md and architecture-changes.md."
        )

    missing: list[str] = []

    try:
        from deepagents import create_deep_agent
    except ImportError:
        missing.append("deepagents.create_deep_agent")
    else:
        if "interrupt_on" not in inspect.signature(create_deep_agent).parameters:
            missing.append("deepagents.create_deep_agent(interrupt_on=...)")

    try:
        from langchain.agents.middleware.human_in_the_loop import (  # noqa: F401
            HumanInTheLoopMiddleware,
            InterruptOnConfig,
        )
    except ImportError:
        missing.append(
            "langchain.agents.middleware.human_in_the_loop.{HumanInTheLoopMiddleware,InterruptOnConfig}"
        )

    try:
        from langchain.agents.middleware import TodoListMiddleware  # noqa: F401
    except ImportError:
        missing.append("langchain.agents.middleware.TodoListMiddleware")

    try:
        from langgraph.types import Command, interrupt  # noqa: F401
    except ImportError:
        missing.append("langgraph.types.{Command,interrupt}")

    if missing:
        sys.exit(
            f"Missing internal(s) this harness's mode gating depends on: {missing!r}. "
            "See poc-wiki/execution-modes/mode-design.md's 'Primitives available' section "
            "and architecture-changes.md."
        )

    from harness.agent import _checkpointer

    if _checkpointer is None:
        sys.exit(
            "harness.agent._checkpointer is None -- langgraph.types.interrupt() requires a "
            "checkpointer to function at all. Mode gating (agent_auto's approval pause) cannot "
            "work without one."
        )
