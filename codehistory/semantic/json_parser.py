"""Public, transport-independent JSON completion helpers."""

import json

from .client import LLMClient


def parse_json(content: str | None) -> dict | None:
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    for marker in ("```json", "```"):
        if marker in content:
            start = content.index(marker) + len(marker)
            end = content.find("```", start)
            if end > start:
                try:
                    return json.loads(content[start:end].strip())
                except json.JSONDecodeError:
                    pass
    return {"raw": content.strip()}


def complete_json(client: LLMClient, prompt: str, **options) -> dict | None:
    return parse_json(client.complete(prompt, **options))
