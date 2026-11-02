"""Domain DTOs for evidence-backed terminology recognition."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TermEvidence:
    evidence_type: str
    evidence_value: str
    weight: float = 0.0
    source_location: dict[str, Any] = field(default_factory=dict)
    rule_id: str = ""
    node_id: str | None = None
    id: str = ""
    # Domain Evidence Graph contract: evidence must stay traceable, dedupable
    # and classifiable as support/against before it can influence a score.
    polarity: str = "SUPPORT"
    reliability: float = 0.0
    independence_group: str = ""
    content_hash: str = ""
    derived_from: str = ""
    source_version: str = ""
    observed_at: str = ""
    subject_id: str = ""
    predicate: str = ""
    object_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "evidence_type": self.evidence_type,
            "evidence_value": self.evidence_value,
            "weight": round(self.weight, 4),
            "source_location": self.source_location,
            "rule_id": self.rule_id,
            "node_id": self.node_id,
            "polarity": self.polarity,
            "reliability": round(self.reliability, 4),
            "independence_group": self.independence_group,
            "content_hash": self.content_hash,
            "derived_from": self.derived_from,
            "source_version": self.source_version,
            "observed_at": self.observed_at,
            "subject_id": self.subject_id,
            "predicate": self.predicate,
            "object_value": self.object_value,
        }


@dataclass
class TermCandidate:
    id: str
    repository_id: str
    snapshot_id: str
    canonical_name: str
    normalized_name: str
    term_type: str
    bounded_context: str
    domain_score: float = 0.0
    confidence_score: float = 0.0
    confidence_band: str = "low"
    status: str = "candidate"
    source: str = "rule"
    definition: str = ""
    mentions: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[TermEvidence] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    # Candidate provenance and governance fields.
    display_name: str = ""
    domain_id: str = ""
    module: str = ""
    language: str = ""
    priority: str = "P1"
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    source_of_truth: str = ""
    term_confidence: float = 0.0
    definition_confidence: float = 0.0
    decision_band: str = "noise"
    lifecycle_status: str = "DISCOVERED"
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "snapshot_id": self.snapshot_id,
            "canonical_name": self.canonical_name,
            "display_name": self.display_name or self.canonical_name,
            "normalized_name": self.normalized_name,
            "term_type": self.term_type,
            "bounded_context": self.bounded_context,
            "domain_id": self.domain_id or self.repository_id,
            "module": self.module,
            "language": self.language,
            "priority": self.priority,
            "source_refs": self.source_refs,
            "source_of_truth": self.source_of_truth,
            "domain_score": round(self.domain_score, 4),
            "confidence_score": round(self.confidence_score, 4),
            "term_confidence": round(self.term_confidence or self.confidence_score, 4),
            "definition_confidence": round(self.definition_confidence, 4),
            "decision_band": self.decision_band,
            "confidence_band": self.confidence_band,
            "status": self.status,
            "lifecycle_status": self.lifecycle_status,
            "version": self.version,
            "source": self.source,
            "definition": self.definition,
            "mentions": self.mentions,
            "evidence": [item.to_dict() for item in self.evidence],
            "evidence_ids": [item.id for item in self.evidence if item.id],
            "aliases": sorted(set(self.aliases)),
            "risk_flags": sorted(set(self.risk_flags)),
        }


@dataclass
class TermRelation:
    source_term_id: str
    target_term_id: str
    relationship: str
    confidence: float
    status: str = "candidate"
    score_breakdown: dict[str, float] = field(default_factory=dict)
    llm_status: str = "not_requested"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_term_id": self.source_term_id,
            "target_term_id": self.target_term_id,
            "relationship": self.relationship,
            "confidence": round(self.confidence, 4),
            "status": self.status,
            "score_breakdown": self.score_breakdown,
            "llm_status": self.llm_status,
        }
