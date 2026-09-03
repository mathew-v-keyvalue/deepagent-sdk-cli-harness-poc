"""Provider-agnostic model resolution.

Part of what this POC is comparing against the sibling Claude SDK POC is
whether leaving Claude Agent SDK buys back multi-vendor flexibility (see
that POC's `CLAUDE_SDK_RECONSIDERATION.md`, "Vendor lock-in": Anthropic
only, LiteLLM request closed "not planned"). `create_deep_agent`'s `model`
parameter accepts a `'provider:model-name'` string passed straight to
LangChain's `init_chat_model` (confirmed directly against the installed
`deepagents`/`langchain` packages — see this repo's README "Confirmed
against the installed package, not docs prose"), so there is nothing
Anthropic-specific to route around here: swapping providers is an env var,
not a code change.
"""

from __future__ import annotations

import os

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

DEFAULT_MODEL = "anthropic:claude-sonnet-4-6"

_REQUIRED_KEY_BY_PROVIDER = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "google-genai": "GOOGLE_API_KEY",
}


def resolve_model() -> BaseChatModel:
    """Build the chat model from `AGENT_MODEL` (default: Anthropic).

    Reads `AGENT_MODEL` fresh on every call rather than caching a module-
    level model instance, so a changed env var takes effect on server
    restart without any other code change — the same "swap a config value,
    not a code path" property `CLAUDE_SDK_RECONSIDERATION.md` says Claude
    SDK does not have.
    """
    model_spec = os.environ.get("AGENT_MODEL", DEFAULT_MODEL)
    provider = model_spec.split(":", 1)[0] if ":" in model_spec else None
    required_key = _REQUIRED_KEY_BY_PROVIDER.get(provider or "")
    if required_key and not os.environ.get(required_key):
        raise RuntimeError(
            f"AGENT_MODEL={model_spec!r} needs {required_key} set in this process's "
            "environment (deployment-wide secret, read at startup — see .env.example)."
        )
    return init_chat_model(model_spec)
