"""Application use cases for evidence-backed terminology."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from ..analysis.knowledge.terms import TermRecognizer
from ..analysis.topology.matching import EntitySimilarity

_ALIGNABLE_TYPES = frozenset({"entity", "resource", "event", "value_object"})
_TECHNICAL_SERVICE_TOKENS = frozenset({
    "admin", "api", "config", "configuration", "consul", "db", "database", "eureka",
    "gateway", "health", "kafka", "logging", "metrics", "monitor", "monitoring", "nacos",
    "observability", "proxy", "rabbitmq", "redis", "registry", "swagger", "trace", "tracing",
    "zookeeper",
})


def _service_tokens(value: str) -> set[str]:
    import re

    return {item.lower() for item in re.findall(r"[A-Za-z\u0080-\uffff]+|\d+", value.replace("_", " ").replace("-", " "))}


def _is_technical_service(service_id: str, display_name: str = "") -> bool:
    return bool(_service_tokens(service_id) & _TECHNICAL_SERVICE_TOKENS) or bool(
        _service_tokens(display_name) & _TECHNICAL_SERVICE_TOKENS
    )


class TermRecognitionService:
    def __init__(self, store, recognizer: TermRecognizer | None = None):
        self.store = store
        self.recognizer = recognizer or TermRecognizer()

    def extract(
        self, snapshot_id: str, repository_id: str, facts: dict[str, Any], term_types: Iterable[str] | None = None
    ) -> dict[str, Any]:
        report = self.recognizer.extract(snapshot_id, repository_id, facts, term_types)
        persisted = self.store.replace_snapshot(snapshot_id, repository_id, report)
        report["persisted_counts"] = persisted
        return report

    def list(self, snapshot_id: str, **filters) -> dict[str, Any]:
        return self.store.list_terms(snapshot_id, **filters)

    def get(self, snapshot_id: str, term_id: str) -> dict[str, Any] | None:
        return self.store.get_term(snapshot_id, term_id)

    def evidence(self, snapshot_id: str, term_id: str) -> list[dict[str, Any]]:
        return self.store.list_evidence(snapshot_id, term_id)

    def review(self, snapshot_id: str, term_id: str, action: str, value: dict[str, Any], reason: str, author: str) -> dict[str, Any]:
        return self.store.review(snapshot_id, term_id, action, value, reason, author)

    def add_manual(self, snapshot_id: str, repository_id: str, value: dict[str, Any]) -> dict[str, Any]:
        return self.store.add_manual(snapshot_id, repository_id, value)

    def save_alignments(self, view_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return self.store.replace_alignments(view_id, result)

    def list_alignments(self, view_id: str, **filters) -> dict[str, Any]:
        return self.store.list_alignments(view_id, **filters)

    def review_alignment(self, view_id: str, alignment_id: str, action: str, reason: str, author: str) -> dict[str, Any]:
        return self.store.review_alignment(view_id, alignment_id, action, reason, author)

    def align(
        self,
        reports: list[dict[str, Any]],
        *,
        service_edges: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Align business terms only across connected, non-technical services."""
        services = []
        for report in reports:
            service_id = report.get("service_id") or report.get("repository_id", "")
            display_name = report.get("display_name") or service_id
            candidates = report.get("default_terms") or report.get("terms") or []
            services.append({
                "service_id": service_id,
                "repository_id": report.get("repository_id", service_id),
                "display_name": display_name,
                "technical": _is_technical_service(service_id, display_name),
                "terms": [item for item in candidates
                          if item.get("term_type") in _ALIGNABLE_TYPES
                          and item.get("status") == "accepted"
                          and "technical_name" not in (item.get("risk_flags") or [])],
            })
        service_by_id = {item["service_id"]: item for item in services}
        excluded_services = [
            {"service_id": item["service_id"], "display_name": item["display_name"], "reason": "technical_service"}
            for item in services if item["technical"]
        ]
        pair_reasons: dict[tuple[str, str], set[str]] = {}
        if service_edges is None:
            service_ids = [item["service_id"] for item in services if not item["technical"]]
            for index, source_id in enumerate(service_ids):
                for target_id in service_ids[index + 1:]:
                    pair_reasons[(source_id, target_id)] = {"explicit_selection"}
        else:
            for edge in service_edges:
                source_id = str(edge.get("source_member_id") or "")
                target_id = str(edge.get("target_member_id") or "")
                if source_id == target_id or source_id not in service_by_id or target_id not in service_by_id:
                    continue
                if service_by_id[source_id]["technical"] or service_by_id[target_id]["technical"]:
                    continue
                pair = tuple(sorted((source_id, target_id)))
                pair_reasons.setdefault(pair, set()).add(str(edge.get("kind") or "business_dependency"))
        service_pairs = [
            {
                "source_service_id": source_id,
                "target_service_id": target_id,
                "relationship": "connected",
                "status": "recommended",
                "reasons": sorted(reasons),
            }
            for (source_id, target_id), reasons in sorted(pair_reasons.items())
        ]
        mappings = []
        alternatives = []
        for pair in service_pairs:
            left = service_by_id[pair["source_service_id"]]
            right = service_by_id[pair["target_service_id"]]
            for source in left["terms"]:
                for target in right["terms"]:
                    if source["term_type"] != target["term_type"]:
                        continue
                    score = EntitySimilarity.score(source["canonical_name"], target["canonical_name"])
                    if score < 0.6:
                        continue
                    relationship = "same" if score >= 0.85 else "related"
                    item = {
                        "id": "align-" + hashlib.sha256(
                            f"{left['service_id']}|{right['service_id']}|{source['id']}|{target['id']}|{relationship}".encode()
                        ).hexdigest()[:24],
                        "source_service_id": left["service_id"],
                        "target_service_id": right["service_id"],
                        "source_repository_id": left["repository_id"],
                        "target_repository_id": right["repository_id"],
                        "source_term_id": source["id"], "target_term_id": target["id"],
                        "relationship": relationship, "confidence": round(score, 4),
                        "status": "needs_review",
                        "score_breakdown": {"name_similarity": round(score, 4), "term_type_match": 1.0},
                        "service_relation_reasons": pair["reasons"],
                    }
                    (mappings if relationship == "same" else alternatives).append(item)
        return {
            "schema_version": "term-alignment/v1",
            "services": [item["service_id"] for item in services],
            "service_pairs": service_pairs,
            "excluded_services": excluded_services,
            "mappings": mappings,
            "alternatives": alternatives,
        }
