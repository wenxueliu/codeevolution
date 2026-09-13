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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "evidence_type": self.evidence_type,
            "evidence_value": self.evidence_value,
            "weight": round(self.weight, 4),
            "source_location": self.source_location,
            "rule_id": self.rule_id,
            "node_id": self.node_id,
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "snapshot_id": self.snapshot_id,
            "canonical_name": self.canonical_name,
            "normalized_name": self.normalized_name,
            "term_type": self.term_type,
            "bounded_context": self.bounded_context,
            "domain_score": round(self.domain_score, 4),
            "confidence_score": round(self.confidence_score, 4),
            "confidence_band": self.confidence_band,
            "status": self.status,
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
