"""Application use cases for evidence-backed terminology."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

import json

from ..analysis.knowledge.definitions import generate_definition
from ..analysis.knowledge.normalization import pair_terms
from ..analysis.knowledge.terms import TERM_ANALYSIS_VERSION, TermRecognizer
from ..analysis.topology.matching import EntitySimilarity
from ..domain.term_types import TERM_API_RESOURCE, TERM_BUSINESS_EVENT, TERM_ENTITY, canonical_term_type

_ALIGNABLE_TYPES = frozenset({TERM_ENTITY, TERM_API_RESOURCE, TERM_BUSINESS_EVENT})
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
    def __init__(self, store, recognizer: TermRecognizer | None = None, llm_client=None):
        self.store = store
        self.recognizer = recognizer or TermRecognizer()
        self.llm_client = llm_client

    def extract(
        self, snapshot_id: str, repository_id: str, facts: dict[str, Any], term_types: Iterable[str] | None = None
    ) -> dict[str, Any]:
        report = self.recognizer.extract(snapshot_id, repository_id, facts, term_types)
        report = self.store.apply_overrides(snapshot_id, repository_id, report)
        persisted = self.store.replace_snapshot(snapshot_id, repository_id, report)
        report["persisted_counts"] = persisted
        return report

    def list(self, snapshot_id: str, **filters) -> dict[str, Any]:
        return self.store.list_terms(snapshot_id, **filters)

    def get(self, snapshot_id: str, term_id: str) -> dict[str, Any] | None:
        return self.store.get_term(snapshot_id, term_id)

    def evidence(self, snapshot_id: str, term_id: str) -> list[dict[str, Any]]:
        return self.store.list_evidence(snapshot_id, term_id)

    def evidence_package(self, snapshot_id: str, term_id: str) -> dict[str, Any] | None:
        return self.store.get_evidence_package(snapshot_id, term_id)

    def conflicts(self, snapshot_id: str) -> list[dict[str, Any]]:
        return self.store.list_conflicts(snapshot_id)

    def assets(self, snapshot_id: str, asset_type: str = "") -> list[dict[str, Any]]:
        return self.store.list_assets(snapshot_id, asset_type)

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

    # ── Concept normalization ──

    def pair_judgments(self, snapshot_id: str) -> dict[str, Any]:
        terms = self.store.list_terms(snapshot_id, limit=500)["terms"]
        evidence = {item["id"]: self.store.list_evidence(snapshot_id, item["id"]) for item in terms}
        conflicts = self._conflicts_by_term(snapshot_id)
        judgments = pair_terms(
            terms,
            evidence_by_term=evidence,
            conflicts_by_term=conflicts,
            analysis_version_by_term={item["id"]: TERM_ANALYSIS_VERSION for item in terms},
        )
        judgments = [_scoped_pair(item, f"snapshot|{snapshot_id}") for item in judgments]
        result = self.store.replace_pair_judgments(snapshot_id, "", judgments)
        return {**result, "term_names": {item["id"]: item.get("canonical_name", "")
                                         for item in terms}}

    def pair_judgments_for_view(self, view_id: str, snapshots: dict[str, str]) -> dict[str, Any]:
        """Compare accepted terms across the member snapshots of a Graph View."""
        terms: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        evidence: dict[str, list[dict[str, Any]]] = {}
        conflicts: dict[str, list[dict[str, Any]]] = {}
        for service_id, snapshot_id in snapshots.items():
            for term in self.store.list_terms(snapshot_id, status="accepted", limit=500)["terms"]:
                terms.append({**term, "service_id": service_id, "snapshot_id": snapshot_id})
                names[term["id"]] = term.get("canonical_name", "")
                evidence[term["id"]] = self.store.list_evidence(snapshot_id, term["id"])
                conflicts[term["id"]] = self._conflicts_by_term(snapshot_id).get(term["id"], [])
        judgments = pair_terms(
            terms, evidence_by_term=evidence, conflicts_by_term=conflicts,
            analysis_version_by_term={item["id"]: TERM_ANALYSIS_VERSION for item in terms},
        )
        judgments = [_scoped_pair(item, f"view|{view_id}") for item in judgments]
        result = self.store.replace_pair_judgments("", view_id, judgments)
        return {**result, "term_names": names}

    def review_pair_judgment(
        self, judgment_id: str, action: str, reason: str = "", reviewer: str = "", role: str = "",
    ) -> dict[str, Any]:
        return self.store.review_pair_judgment(
            judgment_id, action, reason, reviewer, role,
        )

    def merge_terms(
        self, snapshot_id: str, source_term_ids: list[str], target_term_id: str,
        reason: str = "", reviewer: str = "", role: str = "", evidence_refs: list[str] | None = None,
        confirm_hard_constraints: bool = False,
    ) -> dict[str, Any]:
        return self.store.merge_terms(
            snapshot_id, source_term_ids, target_term_id, reason=reason, reviewer=reviewer,
            role=role, evidence_refs=evidence_refs,
            confirm_hard_constraints=confirm_hard_constraints,
        )

    def reverse_merge(self, merge_id: str, reason: str = "", reviewer: str = "", role: str = "") -> dict[str, Any]:
        return self.store.reverse_merge(merge_id, reason=reason, reviewer=reviewer, role=role)

    def list_merges(self, snapshot_id: str = "") -> dict[str, Any]:
        return self.store.list_merges(snapshot_id)

    # ── Semantic definitions ──

    def generate_definition(self, snapshot_id: str, term_id: str) -> dict[str, Any]:
        package = self.store.get_evidence_package(snapshot_id, term_id)
        if package is None:
            raise KeyError(term_id)
        definition = generate_definition(
            package, self.llm_client, analysis_version=TERM_ANALYSIS_VERSION,
        )
        definition["term_id"] = term_id
        return self.store.save_definition(snapshot_id, definition)

    def get_definition(self, snapshot_id: str, term_id: str) -> dict[str, Any] | None:
        return self.store.get_definition(snapshot_id, term_id)

    def list_definitions(self, snapshot_id: str, status: str = "") -> list[dict[str, Any]]:
        return self.store.list_definitions(snapshot_id, status)

    def review_definition(
        self, definition_id: str, action: str, reason: str = "", reviewer: str = "", role: str = "",
    ) -> dict[str, Any]:
        return self.store.review_definition(definition_id, action, reason, reviewer, role)

    # ── Governance and release ──

    def review_records(self, snapshot_id: str = "", item_type: str = "", item_id: str = "") -> dict[str, Any]:
        return self.store.list_review_records(snapshot_id, item_type, item_id)

    def create_release(self, snapshot_id: str, **value) -> dict[str, Any]:
        return self.store.create_release(snapshot_id, **value)

    def list_releases(self) -> dict[str, Any]:
        return self.store.list_releases()

    def get_release(self, release_id: str) -> dict[str, Any] | None:
        return self.store.get_release(release_id)

    def release_diff(self, release_id: str, compare_to: str = "") -> dict[str, Any]:
        return self.store.release_diff(release_id, compare_to)

    # ── Incremental analysis ──

    def analysis_run(
        self, snapshot_id: str, repository_id: str, facts: dict[str, Any], commit_id: str = "",
    ) -> dict[str, Any]:
        input_hash = hashlib.sha256(
            json.dumps(facts, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        run, reused = self.store.create_or_reuse_analysis_run(
            repository_id=repository_id, commit_id=commit_id,
            analysis_version=TERM_ANALYSIS_VERSION, input_hash=input_hash,
            scope={"snapshot_id": snapshot_id},
        )
        if reused:
            return {**run, "reused": True}
        try:
            report = self.extract(snapshot_id, repository_id, facts)
            return {
                **self.store.finish_analysis_run(
                    run["id"], status="completed",
                    result={"counts": report.get("counts", {}),
                            "analysis_version": report.get("analysis_version", "")},
                ),
                "reused": False,
            }
        except Exception as error:
            self.store.finish_analysis_run(run["id"], status="failed", error_message=str(error)[:1000])
            raise

    def get_analysis_run(self, run_id: str) -> dict[str, Any] | None:
        return self.store.get_analysis_run(run_id)

    def _conflicts_by_term(self, snapshot_id: str) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for conflict in self.store.list_conflicts(snapshot_id):
            grouped.setdefault(conflict["term_id"], []).append(conflict)
        return grouped

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
                          if canonical_term_type(item.get("term_type", "")) in _ALIGNABLE_TYPES
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
                    if canonical_term_type(source["term_type"]) != canonical_term_type(target["term_type"]):
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


def _scoped_pair(item: dict[str, Any], scope: str) -> dict[str, Any]:
    """Pair ids must stay unique between a snapshot and a view scope."""
    scoped = dict(item)
    scoped["id"] = "pair-" + hashlib.sha256(
        f"{scope}|{item['id']}".encode("utf-8")
    ).hexdigest()[:24]
    return scoped
