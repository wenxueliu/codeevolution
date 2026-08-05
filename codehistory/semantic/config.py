"""Environment-backed semantic service configuration."""

import os

from ..infrastructure.llm_config_store import LLMConfigStore


def get_environment_llm_config() -> dict | None:
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    model = os.environ.get("CODEHISTORY_LLM_MODEL", "gpt-4o-mini")
    if "claude" in model.lower():
        api_key = os.environ.get("ANTHROPIC_API_KEY", api_key)
    return {
        "api_key": api_key,
        "model": model,
        "api_base": os.environ.get("CODEHISTORY_LLM_BASE", ""),
    }


def get_llm_config() -> dict | None:
    """Resolve environment configuration first, then the page-managed config."""
    return get_environment_llm_config() or LLMConfigStore().load()


def get_llm_config_status() -> dict:
    environment = get_environment_llm_config()
    stored = LLMConfigStore().load()
    effective = environment or stored
    return {
        "available": effective is not None,
        "source": "environment" if environment else ("page" if stored else "none"),
        "model": effective.get("model", "") if effective else "",
        "api_base": effective.get("api_base", "") if effective else "",
        "api_key_configured": bool(effective and effective.get("api_key")),
        "stored_configured": stored is not None,
        "environment_override": environment is not None,
    }
