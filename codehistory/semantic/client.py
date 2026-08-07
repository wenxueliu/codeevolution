"""LLM transport port and OpenAI adapter."""

import json
from typing import Protocol


class LLMClient(Protocol):
    def complete(
        self, prompt: str, max_tokens: int = 800, temperature: float = 0.2
    ) -> str | None: ...


class OpenAILLMClient:
    def __init__(self, config: dict):
        self.config = config

    def complete(self, prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str | None:
        try:
            from openai import OpenAI
        except ImportError:
            return None
        try:
            client = OpenAI(
                api_key=self.config["api_key"],
                base_url=self.config["api_base"] or None,
            )
            response = client.chat.completions.create(
                model=self.config["model"],
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content
        except Exception as error:
            return json.dumps({"error": str(error)})
