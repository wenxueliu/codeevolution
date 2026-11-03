"""Pairwise concept normalization over evidence-backed term candidates.

A judgment is never a merge decision by itself: it records the two term ids,
supporting/against evidence, a confidence, the hard/soft/review constraints it
hit, and the algorithm version so a human review stays reproducible.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ...domain.term_types import (
    TERM_API_RESOURCE,
    TERM_BUSINESS_ACTION,
    TERM_BUSINESS_ATTRIBUTE,
    TERM_BUSINESS_ENUM,
    TERM_BUSINESS_EVENT,
    TERM_COMMAND,
    TERM_DTO,
    TERM_ENTITY,
    TERM_PAGE,
    canonical_term_type,
)
from ..topology.matching import EntitySimilarity

PAIR_ALGORITHM_VERSION = "pair-v1"

SAME_CONCEPT = "SAME_CONCEPT"
ALIAS = "ALIAS"
CONTEXT_PROJECTION = "CONTEXT_PROJECTION"
SPECIALIZATION = "SPECIALIZATION"
RELATED = "RELATED"
DISTINCT = "DISTINCT"
UNCERTAIN = "UNCERTAIN"
PAIR_JUDGMENTS = (
    SAME_CONCEPT, ALIAS, CONTEXT_PROJECTION, SPECIALIZATION, RELATED, DISTINCT, UNCERTAIN,
)
_MERGE_CANDIDATES = {SAME_CONCEPT, ALIAS}

# Different representation roles of the same business object.  These may be
# projections of one concept but are never merged automatically.
_PROJECTION_PAIRS = {
    frozenset({TERM_API_RESOURCE, TERM_ENTITY}),
    frozenset({TERM_DTO, TERM_ENTITY}),
    frozenset({TERM_PAGE, TERM_ENTITY}),
    frozenset({TERM_DTO, TERM_API_RESOURCE}),
}
# Same name, incompatible concept: a human must decide, never the matcher.
_HARD_TYPE_CONFLICTS = {
    frozenset({TERM_ENTITY, TERM_BUSINESS_EVENT}),
    frozenset({TERM_ENTITY, TERM_BUSINESS_ACTION}),
    frozenset({TERM_ENTITY, TERM_COMMAND}),
    frozenset({TERM_ENTITY, TERM_BUSINESS_ENUM}),
    frozenset({TERM_BUSINESS_EVENT, TERM_COMMAND}),
    frozenset({TERM_BUSINESS_ATTRIBUTE, TERM_BUSINESS_EVENT}),
}


def _tokens(value: str) -> set[str]:
    return {item for item in str(value or "").lower().replace("_", " ").replace("-", " ").split() if item}


def _evidence_ref(item: dict[str, Any]) -> dict[str, Any]:
    source = item.get("source_location") or {}
    return {
        "evidence_id": item.get("id", ""),
        "evidence_type": item.get("evidence_type", ""),
        "value": item.get("evidence_value", ""),
        "locator": source,
        "polarity": item.get("polarity", "SUPPORT"),
    }


def _identity_values(term: dict[str, Any], evidence: list[dict[str, Any]]) -> set[str]:
    values = set()
    for item in evidence:
        if item.get("evidence_type") == "identity_key":
            values.add(str(item.get("evidence_value") or "").strip().lower())
    for ref in term.get("source_refs") or []:
        if ref.get("kind") in {"identity_key", "primary_key"} and ref.get("symbol"):
            values.add(str(ref["symbol"]).strip().lower())
    return {item for item in values if item}


def judge_pair(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    left_evidence: list[dict[str, Any]] | None = None,
    right_evidence: list[dict[str, Any]] | None = None,
    left_conflicts: list[dict[str, Any]] | None = None,
    right_conflicts: list[dict[str, Any]] | None = None,
    left_analysis_version: str = "",
    right_analysis_version: str = "",
) -> dict[str, Any]:
    """Judge whether two terms name the same concept, and why."""
    left_type = canonical_term_type(left.get("term_type", ""))
    right_type = canonical_term_type(right.get("term_type", ""))
    left_name = str(left.get("normalized_name") or left.get("canonical_name") or "")
    right_name = str(right.get("normalized_name") or right.get("canonical_name") or "")
    similarity = EntitySimilarity.score(left_name, right_name)
    support: list[dict[str, Any]] = []
    against: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    confidence = similarity

    def constrain(kind: str, code: str, detail: str) -> None:
        constraints.append({"kind": kind, "code": code, "detail": detail})

    if left_name and left_name == right_name:
        support.append({"evidence_id": "", "evidence_type": "normalized_name", "value": left_name})
    if left_type == right_type:
        support.append({"evidence_id": "", "evidence_type": "term_type", "value": left_type})
    else:
        against.append({
            "evidence_id": "", "evidence_type": "term_type",
            "value": f"{left_type}≠{right_type}",
        })

    shared_identity = _identity_values(left, left_evidence or []) & _identity_values(
        right, right_evidence or []
    )
    if shared_identity:
        support.append({
            "evidence_id": "", "evidence_type": "identity_key",
            "value": sorted(shared_identity)[0],
        })
        confidence = min(1.0, confidence + 0.1)

    left_context = str(left.get("bounded_context") or "")
    right_context = str(right.get("bounded_context") or "")
    same_context = not left_context or not right_context or left_context == right_context
    if not same_context:
        constrain("review", "cross_bounded_context", f"{left_context} ≠ {right_context}")
        if not shared_identity:
            constrain(
                "hard", "different_bounded_context_without_mapping",
                "cross-context pair has no identity or contract mapping evidence",
            )

    type_pair = frozenset({left_type, right_type})
    if type_pair in _HARD_TYPE_CONFLICTS:
        constrain("review", "type_conflict", f"{left_type} vs {right_type}")
        against.append({
            "evidence_id": "", "evidence_type": "type_conflict",
            "value": f"{left_type}/{right_type}",
        })
        confidence = min(confidence, 0.4)

    for term, conflicts in ((left, left_conflicts), (right, right_conflicts)):
        unresolved = [item for item in (conflicts or []) if not item.get("resolved")]
        if unresolved:
            against.append({
                "evidence_id": "", "evidence_type": "unresolved_conflict",
                "value": str(unresolved[0].get("conflict_type", "")),
            })
            constrain("review", "unresolved_evidence_conflict", str(term.get("id", "")))
            confidence = min(confidence, 0.5)

    left_lifecycle = str(left.get("lifecycle_status") or "")
    right_lifecycle = str(right.get("lifecycle_status") or "")
    if {left_lifecycle, right_lifecycle} & {"REJECTED", "DEPRECATED", "MERGED"}:
        constrain("hard", "lifecycle_conflict", f"{left_lifecycle} vs {right_lifecycle}")
        confidence = min(confidence, 0.3)

    if left_analysis_version and right_analysis_version and left_analysis_version != right_analysis_version:
        constrain("review", "analysis_version_mismatch", f"{left_analysis_version} vs {right_analysis_version}")
        confidence = min(confidence, 0.6)

    judgment = _judgment(
        left_name, right_name, left_type, right_type, same_context, similarity, constraints,
    )
    # Soft constraints are recorded but only nudge the score; hard constraints
    # always force manual resolution and can never auto-merge.
    if any(item["kind"] == "soft" for item in constraints):
        confidence = max(0.0, confidence - 0.05)
    if any(item["kind"] == "hard" for item in constraints):
        judgment = UNCERTAIN if judgment in _MERGE_CANDIDATES else judgment
    if judgment in {DISTINCT}:
        confidence = min(confidence, 0.3)
    if judgment == UNCERTAIN:
        confidence = min(confidence, 0.5)
    return {
        "id": pair_id(left.get("id", ""), right.get("id", "")),
        "source_term_id": str(left.get("id") or ""),
        "target_term_id": str(right.get("id") or ""),
        "judgment": judgment,
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "support_evidence": support,
        "against_evidence": against,
        "constraints": constraints,
        "algorithm_version": PAIR_ALGORITHM_VERSION,
        "status": "proposed",
        "merge_allowed": (
            judgment in _MERGE_CANDIDATES
            and not any(item["kind"] == "hard" for item in constraints)
        ),
    }


def _judgment(
    left_name: str, right_name: str, left_type: str, right_type: str,
    same_context: bool, similarity: float, constraints: list[dict[str, Any]],
) -> str:
    if any(item["code"] == "lifecycle_conflict" for item in constraints):
        return DISTINCT
    if frozenset({left_type, right_type}) in _HARD_TYPE_CONFLICTS and left_name == right_name:
        return DISTINCT
    if left_name == right_name:
        if not same_context:
            return CONTEXT_PROJECTION
        if left_type != right_type and frozenset({left_type, right_type}) in _PROJECTION_PAIRS:
            return CONTEXT_PROJECTION
        return SAME_CONCEPT if left_type == right_type else RELATED
    if left_type != right_type and frozenset({left_type, right_type}) not in _PROJECTION_PAIRS:
        return DISTINCT if similarity < 0.85 else UNCERTAIN
    left_tokens, right_tokens = _tokens(left_name), _tokens(right_name)
    if left_tokens and right_tokens and (
        left_tokens < right_tokens or right_tokens < left_tokens
    ):
        return SPECIALIZATION
    if similarity >= 0.85:
        return ALIAS
    if similarity >= 0.6:
        return RELATED
    if similarity <= 0.0:
        return DISTINCT
    return UNCERTAIN


def pair_id(left_id: str, right_id: str) -> str:
    ordered = "|".join(sorted((left_id, right_id)))
    return "pair-" + hashlib.sha256(ordered.encode("utf-8")).hexdigest()[:24]


def pair_terms(
    terms: list[dict[str, Any]],
    *,
    evidence_by_term: dict[str, list[dict[str, Any]]] | None = None,
    conflicts_by_term: dict[str, list[dict[str, Any]]] | None = None,
    analysis_version_by_term: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Judge every same-type pair that shares at least one name token."""
    evidence_by_term = evidence_by_term or {}
    conflicts_by_term = conflicts_by_term or {}
    analysis_version_by_term = analysis_version_by_term or {}
    judgments: list[dict[str, Any]] = []
    ordered = sorted(terms, key=lambda item: str(item.get("id") or ""))
    for index, left in enumerate(ordered):
        left_type = canonical_term_type(left.get("term_type", ""))
        left_tokens = _tokens(left.get("normalized_name") or left.get("canonical_name") or "")
        for right in ordered[index + 1:]:
            if left.get("id") == right.get("id"):
                continue
            right_type = canonical_term_type(right.get("term_type", ""))
            right_tokens = _tokens(right.get("normalized_name") or right.get("canonical_name") or "")
            # Same type or a representation projection; other combinations are
            # judged only when the names are identical (type conflicts).
            if left_type != right_type and frozenset({left_type, right_type}) not in _PROJECTION_PAIRS:
                if not (left_tokens & right_tokens):
                    continue
            elif not (left_tokens & right_tokens):
                continue
            judgment = judge_pair(
                left, right,
                left_evidence=evidence_by_term.get(left.get("id", "")),
                right_evidence=evidence_by_term.get(right.get("id", "")),
                left_conflicts=conflicts_by_term.get(left.get("id", "")),
                right_conflicts=conflicts_by_term.get(right.get("id", "")),
                left_analysis_version=analysis_version_by_term.get(left.get("id", ""), ""),
                right_analysis_version=analysis_version_by_term.get(right.get("id", ""), ""),
            )
            judgments.append(judgment)
    return judgments
