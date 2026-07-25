"""LLM-powered feature explanation via litellm.

Optional: requires litellm and OPENAI_API_KEY (or compatible endpoint).
"""

import json
import os
from typing import Any


def _get_llm_config() -> dict | None:
    """Check if LLM is configured. Returns config dict or None."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    return {
        "api_key": api_key,
        "model": os.environ.get("CODEHISTORY_LLM_MODEL", "gpt-4o-mini"),
        "api_base": os.environ.get("CODEHISTORY_LLM_BASE", ""),
    }


def is_available() -> bool:
    return _get_llm_config() is not None


def explain_feature(
    feature_name: str,
    description: str,
    description_zh: str,
    call_chain: list[dict],
    features_context: list[dict] | None = None,
) -> dict | None:
    """Generate a natural language explanation of a feature's implementation.

    Walks the call chain and asks LLM to explain in both English and Chinese.

    Args:
        feature_name: Function name (e.g., 'get_impact_radius')
        description: English description if available
        description_zh: Chinese description if available
        call_chain: List of {from, to, depth} call edges
        features_context: Optional list of related feature names/descriptions
            to give LLM context about callees.

    Returns:
        {"en": "...", "zh": "..."} or None if LLM unavailable.
    """
    config = _get_llm_config()
    if not config:
        return None

    if not call_chain:
        return {
            "en": f"{feature_name} has no internal call chain (no intra-file function calls detected).",
            "zh": f"{feature_name} 无内部调用链（未检测到文件内函数调用）。",
        }

    # Build call chain summary
    chain_lines = []
    for edge in call_chain:
        indent = "  " * edge.get("depth", 0)
        chain_lines.append(f"{indent}{edge['from']} → {edge['to']}()")

    chain_text = "\n".join(chain_lines)

    # Build context about callees if available
    callee_context = ""
    if features_context:
        callee_lines = []
        for f in features_context[:10]:
            callee_lines.append(f"- {f.get('canonical_name', '')}: {f.get('description', '')}")
        callee_context = "\n".join(callee_lines)

    prompt = f"""You are analyzing code evolution. Explain the implementation of this feature by walking through its call chain.

Feature: {feature_name}
Description: {description}

Call chain (caller → callee, indented by call depth):
{chain_text}

{f'Related functions for context:\n{callee_context}' if callee_context else ''}

Explain in 3-5 sentences:
1. What this feature does at a high level
2. How it works step by step through the call chain
3. Any notable implementation patterns

Output as JSON: {{"en": "English explanation", "zh": "Chinese explanation"}}

JSON:"""

    try:
        import litellm

        response = litellm.completion(
            model=config["model"],
            messages=[{"role": "user", "content": prompt}],
            api_key=config["api_key"],
            api_base=config["api_base"] or None,
            temperature=0.3,
            max_tokens=500,
        )
        content = response.choices[0].message.content
        # Parse JSON from response
        return _parse_json_response(content)
    except ImportError:
        return None
    except Exception as e:
        return {"en": f"LLM error: {e}", "zh": f"LLM 错误: {e}"}


def _parse_json_response(content: str) -> dict | None:
    """Extract JSON from LLM response."""
    if not content:
        return None
    # Try direct parse
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # Try to find JSON block
    for marker in ("```json", "```"):
        if marker in content:
            start = content.index(marker) + len(marker)
            end = content.find("```", start)
            if end > start:
                try:
                    return json.loads(content[start:end].strip())
                except json.JSONDecodeError:
                    pass
    return {"en": content.strip(), "zh": content.strip()}
