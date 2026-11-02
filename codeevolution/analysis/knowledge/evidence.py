"""Evidence fusion and the three independent confidence dimensions.

Evidence is deduplicated by content hash and aggregated per independence
group: the strongest item in a group counts in full, repeated items from the
same group add half weight so one source cannot inflate its own score.
"""

from __future__ import annotations

import hashlib

_EVIDENCE_CATEGORY = {
    "api_route": "Contract",
    "api_schema": "Contract",
    "api_method": "Contract",
    "api_handler": "Behavior",
    "api_entity_link": "Identity",
    "api_model_reference": "Identity",
    "entity_definition": "Structural",
    "structural_type": "Structural",
    "field_structure": "Structural",
    "annotation": "Structural",
    "type_relationship": "Structural",
    "db_table": "Persistence",
    "db_column": "Persistence",
    "identity_key": "Identity",
    "enum_definition": "Structural",
    "enum_members": "Lifecycle",
    "message_contract": "Event",
    "message_role": "Event",
    "event_definition": "Event",
    "command_definition": "Behavior",
    "dto_definition": "Structural",
    "service_method": "Behavior",
    "service_visibility": "Behavior",
    "attribute_definition": "Structural",
    "entity_field_link": "Persistence",
    "frontend_page": "Contract",
    "page_title": "Documentation",
    "manual": "Ownership",
    "user_override": "Ownership",
}
_RELIABILITY = {
    "api_route": 1.0,
    "api_schema": 0.90,
    "api_method": 0.85,
    "api_handler": 0.90,
    "api_entity_link": 0.80,
    "api_model_reference": 0.75,
    "entity_definition": 0.95,
    "structural_type": 0.80,
    "field_structure": 0.75,
    "annotation": 0.60,
    "type_relationship": 0.60,
    "db_table": 1.0,
    "db_column": 0.95,
    "identity_key": 1.0,
    "enum_definition": 0.95,
    "enum_members": 0.80,
    "message_contract": 0.95,
    "message_role": 0.80,
    "event_definition": 0.90,
    "command_definition": 0.85,
    "dto_definition": 0.70,
    "service_method": 0.75,
    "service_visibility": 0.60,
    "attribute_definition": 0.80,
    "entity_field_link": 0.70,
    "frontend_page": 0.80,
    "page_title": 0.75,
    "manual": 1.0,
    "user_override": 1.0,
}
_TYPE_RELIABILITY_OVERRIDES = {
    "DTO": {"structural_type": 0.55, "dto_definition": 0.80},
    "ENTITY": {"structural_type": 0.90},
    "BUSINESS_EVENT": {"event_definition": 0.90, "message_contract": 0.95},
    "BUSINESS_ACTION": {"service_method": 0.70, "api_handler": 0.90},
}
_DECISION_BANDS = (
    (0.90, "auto_suggest"),
    (0.70, "review"),
    (0.40, "supplement"),
)


def evidence_category(evidence_type: str) -> str:
    return _EVIDENCE_CATEGORY.get(evidence_type, "Structural")


def evidence_reliability(term_type: str, evidence_type: str) -> float:
    override = _TYPE_RELIABILITY_OVERRIDES.get(term_type, {})
    return override.get(evidence_type, _RELIABILITY.get(evidence_type, 0.5))


def content_hash(evidence_type: str, value: str, node_id: str | None = None) -> str:
    return hashlib.sha256(
        f"{evidence_type}|{value}|{node_id or ''}".encode("utf-8")
    ).hexdigest()[:24]


def fuse(term_type: str, evidence: list) -> list:
    """Return deduplicated evidence with polarity, reliability and group set."""
    unique: dict[str, object] = {}
    for item in evidence:
        item.polarity = item.polarity if item.polarity in {"SUPPORT", "AGAINST"} else "SUPPORT"
        item.independence_group = item.independence_group or evidence_category(item.evidence_type)
        item.reliability = item.reliability or evidence_reliability(term_type, item.evidence_type)
        item.content_hash = item.content_hash or content_hash(
            item.evidence_type, item.evidence_value, item.node_id
        )
        kept = unique.get(item.content_hash)
        if kept is None or item.weight > kept.weight:
            unique[item.content_hash] = item
    return list(unique.values())


def term_confidence(candidate) -> float:
    groups: dict[str, list[float]] = {}
    for item in candidate.evidence:
        sign = 1.0 if item.polarity == "SUPPORT" else -1.0
        groups.setdefault(item.independence_group, []).append(sign * item.weight * item.reliability)
    score = 0.0
    for contributions in groups.values():
        ordered = sorted(contributions, key=lambda value: -abs(value))
        score += ordered[0] + 0.5 * sum(ordered[1:])
    return min(1.0, max(0.0, score + min(candidate.domain_score * 0.12, 0.12)))


def decision_band(score: float) -> str:
    for threshold, band in _DECISION_BANDS:
        if score >= threshold:
            return band
    return "noise"
