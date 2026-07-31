"""LLM transport port and LiteLLM adapter."""

import json
from typing import Protocol


class LLMClient(Protocol):
    def complete(
        self, prompt: str, max_tokens: int = 800, temperature: float = 0.2
    ) -> str | None: ...


class LiteLLMClient:
    def __init__(self, config: dict):
        self.config = config

    def complete(
        self, prompt: str, max_tokens: int = 800, temperature: float = 0.2
    ) -> str | None:
        try:
            import litellm
        except ImportError:
            return None
        try:
            response = litellm.completion(
                model=self.config["model"],
                messages=[{"role": "user", "content": prompt}],
                api_key=self.config["api_key"],
                api_base=self.config["api_base"] or None,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content
        except Exception as error:
            return json.dumps({"error": str(error)})

