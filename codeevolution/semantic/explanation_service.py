"""LLM prompts for chunk, node, and bottom-up API explanations."""

from __future__ import annotations

import json
from typing import Any

from .json_parser import complete_json

FACT_FIELDS = (
    "business_flow",
    "business_rules",
    "state_changes",
    "side_effects",
    "exceptions",
)


def normalize_explanation(value: dict | None, fallback_summary: str = "") -> dict[str, Any]:
    """Return the stable explanation shape expected by storage and the UI."""
    value = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {"summary": str(value.get("summary") or fallback_summary)}
    for field in FACT_FIELDS:
        raw = value.get(field) or []
        result[field] = raw if isinstance(raw, list) else [str(raw)]
    return result


class ExplanationSemanticService:
    """Translate source bottom-up while keeping every result machine-readable."""

    def __init__(self, client):
        self.client = client

    def explain_chunk(self, node: dict, chunk: dict) -> dict:
        prompt = f"""You are translating source code into auditable business behavior.
Explain only the supplied code block. Do not infer behavior outside these lines.

Function: {node.get('qualified_name') or node.get('name')}
File: {node.get('file')}:{chunk.get('line_start')}-{chunk.get('line_end')}
Source:
```
{chunk.get('source', '')}
```

Return JSON only:
{{
  "summary": "concise business meaning",
  "business_flow": ["ordered actions"],
  "business_rules": [{{"text": "rule", "evidence_lines": [1]}}],
  "state_changes": ["state changes"],
  "side_effects": ["database, message, or external effects"],
  "exceptions": ["failure and exceptional paths"]
}}"""
        result = complete_json(self.client, prompt, max_tokens=900, temperature=0.2)
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError((result or {}).get("error") or "模型未返回结构化代码块解释")
        return normalize_explanation(result)

    def synthesize_local(self, node: dict, chunks: list[dict]) -> dict:
        completed = [chunk["explanation"] for chunk in chunks if chunk.get("explanation")]
        if len(completed) == 1:
            return normalize_explanation(completed[0])
        prompt = f"""Combine the ordered code-block explanations into one explanation of the
current function only. Deduplicate facts, preserve branches and failures, and do not include
behavior that belongs only to called functions.

Function: {node.get('qualified_name') or node.get('name')}
Block explanations:
{json.dumps(completed, ensure_ascii=False)}

Return JSON only with summary, business_flow, business_rules, state_changes,
side_effects, and exceptions arrays."""
        result = complete_json(self.client, prompt, max_tokens=1200, temperature=0.2)
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError((result or {}).get("error") or "模型未返回结构化节点解释")
        return normalize_explanation(result)

    def aggregate_node(self, node: dict, local: dict, children: list[dict]) -> dict:
        if not children:
            return {**normalize_explanation(local), "children": []}
        compact_children = [
            {
                "node_key": child["node_key"],
                "call_line": child.get("call_line"),
                "summary": child.get("aggregate", {}).get("summary", ""),
                **{
                    field: child.get("aggregate", {}).get(field, [])
                    for field in FACT_FIELDS
                },
            }
            for child in children
        ]
        prompt = f"""Create the bottom-up business explanation for this function and everything
it invokes. Preserve the current function's own behavior, then integrate child behavior in call
context. Deduplicate facts. Never claim an unresolved child was analyzed.

Current function: {node.get('qualified_name') or node.get('name')}
Current-function explanation:
{json.dumps(local, ensure_ascii=False)}
Direct child explanations:
{json.dumps(compact_children, ensure_ascii=False)}

Return JSON only with summary, business_flow, business_rules, state_changes,
side_effects, and exceptions arrays."""
        result = complete_json(self.client, prompt, max_tokens=1800, temperature=0.2)
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError((result or {}).get("error") or "模型未返回结构化聚合解释")
        normalized = normalize_explanation(result)
        normalized["children"] = [
            {"node_key": child["node_key"], "call_line": child.get("call_line")}
            for child in children
        ]
        return normalized
