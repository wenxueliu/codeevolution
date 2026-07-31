"""Environment-backed semantic service configuration."""

import os


def get_llm_config() -> dict | None:
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
