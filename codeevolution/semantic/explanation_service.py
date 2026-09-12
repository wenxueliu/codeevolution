"""LLM prompts for chunk, node, and bottom-up API explanations."""

from __future__ import annotations

import json
from typing import Any

from .json_parser import complete_json
from .explanation_templates import (
    DEFAULT_COMPLEXITY_INSTRUCTIONS,
    normalize_prompt_templates,
    render_prompt,
)

FACT_FIELDS = (
    "business_flow",
    "business_rules",
    "state_changes",
    "side_effects",
    "exceptions",
)

AGENT_FIELDS = (
    "inputs",
    "outputs",
    "preconditions",
    "alternative_flows",
    "dependencies",
    "integration_contracts",
    "node_refs",
    "uncertainties",
)


def normalize_explanation(value: dict | None, fallback_summary: str = "") -> dict[str, Any]:
    """Return the stable explanation shape expected by storage and the UI."""
    value = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {"summary": str(value.get("summary") or fallback_summary)}
    for field in FACT_FIELDS:
        raw = value.get(field) or []
        result[field] = raw if isinstance(raw, list) else [str(raw)]
    for field in AGENT_FIELDS:
        raw = value.get(field) or []
        result[field] = raw if isinstance(raw, list) else [raw]
    for key, raw in value.items():
        if key not in result:
            result[key] = raw
    return result


class ExplanationSemanticService:
    """Translate source bottom-up while keeping every result machine-readable."""

    def __init__(self, client):
        self.client = client

    def explain_chunk(
        self,
        node: dict,
        chunk: dict,
        guidance: str = "",
        templates: dict[str, str] | None = None,
    ) -> dict:
        template = normalize_prompt_templates(templates)["local"]
        prompt = render_prompt(
            template,
            qualified_name=node.get("qualified_name") or node.get("name"),
            service=node.get("service", ""),
            member=node.get("member", ""),
            file=node.get("file", ""),
            line_start=chunk.get("line_start"),
            line_end=chunk.get("line_end"),
            analysis_scope=chunk.get("analysis_scope") or "当前代码块",
            complexity_level=node.get("complexity_level", "medium"),
            complexity_instruction=DEFAULT_COMPLEXITY_INSTRUCTIONS.get(
                node.get("complexity_level", "medium"), DEFAULT_COMPLEXITY_INSTRUCTIONS["medium"]
            ),
            source=chunk.get("source", ""),
            guidance=guidance or "（无）",
        )
        result = complete_json(self.client, prompt, max_tokens=900, temperature=0.2)
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError((result or {}).get("error") or "模型未返回结构化代码块解释")
        return normalize_explanation(result)

    def synthesize_local(
        self,
        node: dict,
        chunks: list[dict],
        guidance: str = "",
        templates: dict[str, str] | None = None,
    ) -> dict:
        completed = [chunk["explanation"] for chunk in chunks if chunk.get("explanation")]
        if len(completed) == 1:
            return normalize_explanation(completed[0])
        prompt = render_prompt(
            normalize_prompt_templates(templates)["synthesis"],
            qualified_name=node.get("qualified_name") or node.get("name"),
            file=node.get("file", ""),
            chunk_explanations=json.dumps(completed, ensure_ascii=False),
            guidance=guidance or "（无）",
        )
        result = complete_json(self.client, prompt, max_tokens=1200, temperature=0.2)
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError((result or {}).get("error") or "模型未返回结构化节点解释")
        return normalize_explanation(result)

    def aggregate_node(
        self,
        node: dict,
        local: dict,
        children: list[dict],
        guidance: str = "",
        templates: dict[str, str] | None = None,
    ) -> dict:
        if not children:
            return {**normalize_explanation(local), "children": []}
        compact_children = [
            {
                "node_key": child["node_key"],
                "call_line": child.get("call_line"),
                "summary": child.get("aggregate", {}).get("summary", ""),
                **{
                    field: child.get("aggregate", {}).get(field, [])
                    for field in (*FACT_FIELDS, *AGENT_FIELDS)
                },
            }
            for child in children
        ]
        prompt = render_prompt(
            normalize_prompt_templates(templates)["aggregate"],
            qualified_name=node.get("qualified_name") or node.get("name"),
            local_explanation=json.dumps(local, ensure_ascii=False),
            children_explanations=json.dumps(compact_children, ensure_ascii=False),
            call_edges=json.dumps(
                [
                    {
                        "node_key": child["node_key"],
                        "call_line": child.get("call_line"),
                    }
                    for child in children
                ],
                ensure_ascii=False,
            ),
            guidance=guidance or "（无）",
        )
        result = complete_json(self.client, prompt, max_tokens=1800, temperature=0.2)
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError((result or {}).get("error") or "模型未返回结构化聚合解释")
        normalized = normalize_explanation(result)
        normalized["children"] = [
            {"node_key": child["node_key"], "call_line": child.get("call_line")}
            for child in children
        ]
        return normalized
