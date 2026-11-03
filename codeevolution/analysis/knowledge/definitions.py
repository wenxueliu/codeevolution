"""Evidence-package-only business definitions with programmatic grounding.

The model never sees the repository: it receives the frozen Evidence Package
and may only restate what the evidence says.  Every FACT must cite a real
evidence id; a definition that fails grounding stays a draft and cannot be
released.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

PROMPT_VERSION = "term-definition-v1"
DEFINITION_SCHEMA_VERSION = "term-definition/v1"
SENSITIVE_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|password|passwd|token|authorization)\s*[:=]\s*\S+"
)
_SECRET_PATTERN = SENSITIVE_PATTERN


class DefinitionGenerationError(ValueError):
    """The model did not return a usable structured definition."""


def build_prompt(package: dict[str, Any]) -> str:
    """Serialize the evidence package into a bounded, sanitized prompt."""
    payload = sanitize_package(package)
    return (
        "You are a domain terminology analyst. Use ONLY the evidence package below.\n"
        "Do not add industry knowledge that the evidence does not support.\n"
        "Return one JSON object with keys: canonical_name, term_type, "
        "business_definition, business_context, attributes, lifecycle, operations, "
        "events, relationships, facts, inferences, unknowns.\n"
        "Every fact must be {\"claim\": str, \"evidence_ids\": [str]} and cite real "
        "evidence ids from the package. Every inference must be "
        "{\"claim\": str, \"supporting_evidence_ids\": [str]}. Unknowns are strings. "
        "If the evidence is insufficient, use unknowns instead of guessing.\n"
        "Do not merge terms, do not change source_of_truth, do not publish.\n"
        f"EVIDENCE_PACKAGE:\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def sanitize_package(package: dict[str, Any]) -> dict[str, Any]:
    """Drop secret-looking values before the package leaves the process."""
    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return _SECRET_PATTERN.sub(r"\1=[REDACTED]", value)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        return value

    return clean(package)


def input_hash(package: dict[str, Any]) -> str:
    encoded = json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def generate_definition(
    package: dict[str, Any], llm_client, *, analysis_version: str = "",
) -> dict[str, Any]:
    """Generate a definition draft and run the four grounding checks."""
    if llm_client is None:
        raise DefinitionGenerationError("LLM is not configured")
    content = llm_client.complete(
        build_prompt(package), max_tokens=1600, temperature=0.2,
        extra={"response_format": {"type": "json_object"}},
    )
    output = _parse_output(content)
    grounding = ground_definition(
        package, output,
        analysis_version=analysis_version,
        unresolved_conflicts=[
            item for item in package.get("conflicts", []) if not item.get("resolved")
        ],
    )
    term = package.get("term") or {}
    return {
        "schema_version": DEFINITION_SCHEMA_VERSION,
        "term_id": str(term.get("id") or ""),
        "canonical_name": str(output.get("canonical_name") or term.get("canonical_name") or ""),
        "definition": str(output.get("business_definition") or ""),
        "term_type": str(output.get("term_type") or term.get("term_type") or ""),
        "business_context": str(output.get("business_context") or ""),
        "attributes": _string_list(output.get("attributes")),
        "lifecycle": _string_list(output.get("lifecycle")),
        "operations": _string_list(output.get("operations")),
        "events": _string_list(output.get("events")),
        "relationships": _string_list(output.get("relationships")),
        "facts": _claims(output.get("facts"), "evidence_ids"),
        "inferences": _claims(output.get("inferences"), "supporting_evidence_ids"),
        "unknowns": _string_list(output.get("unknowns")),
        "evidence_ids": sorted({
            evidence_id
            for fact in _claims(output.get("facts"), "evidence_ids")
            for evidence_id in fact.get("evidence_ids", [])
        }),
        "confidence": round(grounding["confidence"], 4),
        "grounding": grounding,
        "model_name": str(
            getattr(llm_client, "model_name", "")
            or (getattr(llm_client, "config", {}) or {}).get("model", "")
        ),
        "prompt_version": PROMPT_VERSION,
        "analysis_version": analysis_version,
        "input_hash": input_hash(package),
        "status": "pending_review" if grounding["passed"] else "draft",
        "structured_output": output,
    }


def ground_definition(
    package: dict[str, Any], output: dict[str, Any], *,
    analysis_version: str = "",
    unresolved_conflicts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evidence Existence / Version Consistency / Locator / Contradiction."""
    known = {
        str(item.get("id") or ""): item
        for item in list(package.get("support") or []) + list(package.get("against") or [])
        if item.get("id")
    }
    facts = _claims(output.get("facts"), "evidence_ids")
    missing = sorted({
        evidence_id
        for fact in facts
        for evidence_id in fact.get("evidence_ids", [])
        if evidence_id not in known
    })
    evidence_existence = {"passed": bool(facts) and not missing, "missing_evidence_ids": missing}

    package_version = str(package.get("source_version") or "")
    package_analysis = str(package.get("analysis_version") or "")
    version_issues = []
    if not package_version:
        version_issues.append("package source_version is empty")
    if analysis_version and package_analysis and package_analysis != analysis_version:
        version_issues.append(f"package {package_analysis} ≠ analysis {analysis_version}")
    version_consistency = {"passed": not version_issues, "issues": version_issues}

    locator_issues = []
    for evidence_id in sorted({
        evidence_id for fact in facts for evidence_id in fact.get("evidence_ids", [])
    }):
        item = known.get(evidence_id)
        if item is None:
            continue
        locator = item.get("source_location") or {}
        if not (locator.get("file_path") or locator.get("path") or item.get("node_id")):
            locator_issues.append(evidence_id)
    locator_validity = {"passed": not locator_issues, "unresolved_evidence_ids": locator_issues}

    conflicts = unresolved_conflicts or []
    contradiction = {
        "passed": not conflicts,
        "unresolved_conflict_count": len(conflicts),
        "against_evidence_count": len(package.get("against") or []),
    }
    checks = {
        "evidence_existence": evidence_existence,
        "version_consistency": version_consistency,
        "locator_validity": locator_validity,
        "contradiction_check": contradiction,
    }
    passed = all(item["passed"] for item in checks.values())
    # Confidence degrades with each failed check; a grounding pass never makes
    # an inference more certain than the term itself.
    term_confidence = float((package.get("term") or {}).get("term_confidence") or 0)
    failed = sum(not item["passed"] for item in checks.values())
    confidence = max(0.0, min(1.0, term_confidence * (1.0 - 0.2 * failed)))
    return {
        "passed": passed,
        "checks": checks,
        "failed_checks": [name for name, item in checks.items() if not item["passed"]],
        "confidence": confidence,
    }


def _parse_output(content: str | None) -> dict[str, Any]:
    if not content:
        raise DefinitionGenerationError("LLM returned an empty response")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise DefinitionGenerationError("LLM response is not valid JSON") from error
    if not isinstance(value, dict):
        raise DefinitionGenerationError("LLM response must be a JSON object")
    return value


def _claims(value: Any, id_key: str) -> list[dict[str, Any]]:
    rows = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            item = {"claim": item, id_key: []}
        if not isinstance(item, dict) or not item.get("claim"):
            continue
        ids = item.get(id_key) if isinstance(item.get(id_key), list) else []
        rows.append({"claim": str(item["claim"]), id_key: [str(entry) for entry in ids]})
    return rows


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]
