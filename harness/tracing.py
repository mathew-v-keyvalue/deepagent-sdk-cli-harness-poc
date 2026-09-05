"""Netra span/tracing setup — the observability counterpart to
`harness/observability.py`'s logging setup.

Ported from the pattern used by the sibling `cybersierra-ai-agents` repo's
`questionnaire_handler/search/reranker.py`: `netra` is an optional dependency
at the *behavior* level even though it's a hard install (see pyproject.toml) —
if the package is missing, or tracing isn't explicitly enabled via env vars,
every `Netra.start_span(...)` call below degrades to a no-op context manager
that still supports `.set_attribute()`/`.set_success()`/`.set_error()`, so
`harness/sandbox.py`, `harness/executor_tool.py`, and `harness/agent.py` never
need their own conditional logic — they just always call `Netra.start_span`.

Unlike the reference repo, where this guard is duplicated independently
inside `reranker.py`, this harness has three consumer modules, so the guard
lives here once and is imported by all three.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("harness.tracing")
logger.addHandler(logging.NullHandler())

try:
    from netra import Netra, SpanType

    NETRA_AVAILABLE = True
except ImportError:
    NETRA_AVAILABLE = False

    class SpanType:  # type: ignore[no-redef]
        TOOL = "TOOL"

    class _NoOpSpan:
        def set_attribute(self, *args, **kwargs) -> "_NoOpSpan":
            return self

        def set_success(self) -> "_NoOpSpan":
            return self

        def set_error(self, *args, **kwargs) -> "_NoOpSpan":
            return self

        def __enter__(self) -> "_NoOpSpan":
            return self

        def __exit__(self, *args) -> bool:
            return False

    class Netra:  # type: ignore[no-redef]
        @classmethod
        def start_span(cls, *args, **kwargs) -> "_NoOpSpan":
            return _NoOpSpan()


_CONFIGURED = False


def _is_truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def init_tracing() -> None:
    """Idempotent, same shape as `configure_logging()` — safe to call from
    both `server/app.py` and `harness/agent.py`'s `__main__` demo.

    Env vars (all optional, all off by default):
        NETRA_TRACING — must be truthy to enable export at all.
        NETRA_API_KEY — required alongside NETRA_TRACING; without it, tracing
            stays disabled (same as the reference repo's `server.py` gate).
        NETRA_TRACE_CONTENT — whether captured spans include raw content
            (prompts/outputs) vs. metadata only.

    If `netra-sdk` isn't installed, or either of the two required settings
    above is missing, every `Netra.start_span(...)` call in this harness
    silently becomes a no-op — nothing here ever raises for "tracing is
    off."
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    if not NETRA_AVAILABLE:
        logger.warning(
            "netra_unavailable reason=%r",
            "netra-sdk not installed",
            extra={"event": "netra_unavailable", "reason": "netra-sdk not installed"},
        )
        return

    api_key = os.environ.get("NETRA_API_KEY")
    enabled = _is_truthy(os.environ.get("NETRA_TRACING", ""))
    if not (enabled and api_key):
        logger.warning(
            "netra_disabled tracing_enabled=%s api_key_set=%s",
            enabled,
            bool(api_key),
            extra={"event": "netra_disabled", "tracing_enabled": enabled, "api_key_set": bool(api_key)},
        )
        return

    from netra.instrumentation.instruments import InstrumentSet

    Netra.init(
        app_name="cybersierra-deepagents-poc",
        headers=f"x-api-key={api_key}",
        environment=os.environ.get("PLATFORM_ENV", "development"),
        trace_content=_is_truthy(os.environ.get("NETRA_TRACE_CONTENT", "")),
        instruments={InstrumentSet.FASTAPI, InstrumentSet.LANGCHAIN},
    )
    logger.info(
        "netra_initialized",
        extra={"event": "netra_initialized"},
    )
