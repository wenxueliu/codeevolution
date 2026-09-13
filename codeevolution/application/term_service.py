"""Application use cases for evidence-backed terminology."""

from __future__ import annotations

from typing import Any, Iterable

from ..analysis.knowledge.terms import TermRecognizer
from ..analysis.topology.matching import EntitySimilarity


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

    def align(self, reports: list[dict[str, Any]]) -> dict[str, Any]:
        """Return deterministic cross-service mappings without forcing a merge."""
        services = []
        for report in reports:
            candidates = report.get("default_terms") or report.get("terms") or []
            services.append({
                "repository_id": report.get("repository_id", ""),
                "terms": [item for item in candidates if item.get("term_type") == "entity" and item.get("status") == "accepted"],
            })
        mappings = []
        alternatives = []
        for left_index, left in enumerate(services):
            for right in services[left_index + 1:]:
                if left["repository_id"] == right["repository_id"]:
                    continue
                for source in left["terms"]:
                    for target in right["terms"]:
                        score = EntitySimilarity.score(source["canonical_name"], target["canonical_name"])
                        if score < 0.6:
                            continue
                        relationship = "same" if score >= 0.85 else "related"
                        item = {
                            "source_repository_id": left["repository_id"],
                            "target_repository_id": right["repository_id"],
                            "source_term_id": source["id"], "target_term_id": target["id"],
                            "relationship": relationship, "confidence": round(score, 4),
                            "status": "accepted" if relationship == "same" else "needs_review",
                            "score_breakdown": {"name_similarity": round(score, 4)},
                        }
                        (mappings if relationship == "same" else alternatives).append(item)
        return {
            "schema_version": "term-alignment/v1",
            "services": [item["repository_id"] for item in services],
            "mappings": mappings,
            "alternatives": alternatives,
        }
