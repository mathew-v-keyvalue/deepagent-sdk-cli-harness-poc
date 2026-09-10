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

        # harness/agent.py calls these unconditionally too (session grouping,
        # root input/output) — same "never needs its own conditional logic"
        # contract as start_span above.
        @classmethod
        def set_session_id(cls, *args, **kwargs) -> None:
            return None

        @classmethod
        def set_root_input(cls, *args, **kwargs) -> None:
            return None

        @classmethod
        def set_root_output(cls, *args, **kwargs) -> None:
            return None


_CONFIGURED = False


def _is_truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def trace_content_enabled() -> bool:
    """Same gate `init_tracing()` passes to `Netra.init(trace_content=...)`
    for the auto-instrumented LLM spans, exposed here so the three custom
    spans this harness creates by hand (`Agent_Turn` in harness/agent.py,
    `Plan_Step` in harness/executor_tool.py, `cli_call` in harness/sandbox.py)
    can honor the identical setting before attaching prompt/response/
    command-output content to a span — `Netra.init()`'s own `trace_content`
    has no effect on spans WE create via `Netra.start_span(...)` directly,
    so without this, those three span types would leak content externally
    even with `NETRA_TRACE_CONTENT=0`. Read fresh each call (like
    `harness/model.py`'s `resolve_model()`), not cached, so it stays
    consistent with whatever `init_tracing()` decided at startup.
    """
    return _is_truthy(os.environ.get("NETRA_TRACE_CONTENT", ""))


def init_tracing(app_name: str = "cybersierra-deepagents-poc") -> None:
    """Idempotent, same shape as `configure_logging()` — safe to call from
    `server/app.py`, `harness/agent.py`'s `__main__` demo, and
    `eval/netra/run.py` (with a distinct `app_name` — see that module's own
    docstring for why eval traces get a different app_name than the live
    server's, and note that whichever caller runs first in a given process
    wins, per the idempotent guard below).

    Env vars (all optional, all off by default):
        NETRA_TRACING — must be truthy to enable export at all.
        NETRA_API_KEY — required alongside NETRA_TRACING; without it, tracing
            stays disabled (same as the reference repo's `server.py` gate).
        NETRA_TRACE_CONTENT — whether captured spans include raw content
            (prompts/outputs) vs. metadata only.
        NETRA_OTLP_ENDPOINT — read directly by netra-sdk (not this module);
            without it, netra-sdk silently falls back to a ConsoleSpanExporter
            and spans print to stdout instead of reaching the Netra
            dashboard. See .env.example for the shared collector URL.

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
        app_name=app_name,
        headers=f"x-api-key={api_key}",
        environment=os.environ.get("PLATFORM_ENV", "development"),
        trace_content=_is_truthy(os.environ.get("NETRA_TRACE_CONTENT", "")),
        instruments={InstrumentSet.FASTAPI, InstrumentSet.LANGCHAIN},
        # Confirmed by reading the installed opentelemetry-instrumentation-
        # langchain + deepagents/langgraph source directly, not guessed:
        # deepagents builds every graph node as a `RunnableCallable(...,
        # trace=False)` (langchain/agents/factory.py), so node/runnable
        # wrapper spans never fire in this stack today — the "model"/
        # "tools"/"*.before_model" etc. entries below are purely defensive,
        # in case a future deepagents/langgraph version flips that back on.
        # The one span that DOES fire and IS pure noise: LangGraph's own
        # top-level compiled-graph span, named "LangGraph" by default
        # (create_deep_agent() is called with no `name=` in _build_agent
        # above) — it duplicates our own "Agent_Turn" span below it in the
        # tree. Blocking it (children reparent onto the surviving ancestor,
        # nothing is lost) is what actually declutters the dashboard; real
        # LLM-call spans (named after the model class, e.g. "ChatOpenAI")
        # and our own Agent_Turn/Plan_Step/cli_call spans are untouched.
        blocked_spans=[
            "LangGraph",
            "model",
            "tools",
            "*.before_model",
            "*.after_model",
            "*.before_agent",
            "*.after_agent",
            "RunnableSequence*",
            "RunnableLambda*",
            "RunnableBinding*",
            "RunnableParallel*",
            "ChatPromptTemplate*",
            "StrOutputParser*",
        ],
    )
    logger.info(
        "netra_initialized",
        extra={"event": "netra_initialized"},
    )
