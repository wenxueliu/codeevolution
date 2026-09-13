"""Deterministic, evidence-backed terminology candidate extraction.

This module deliberately consumes frozen knowledge facts instead of reading a
live checkout.  It does not ask an LLM to invent terms: the LLM integration,
when added, can only adjudicate candidates produced here.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

from ...domain.terms import TermCandidate, TermEvidence, TermRelation

_GENERIC_TYPE_NAMES = {
    "any", "bool", "boolean", "commonpage", "commonresult", "dict", "double",
    "float", "int", "integer", "list", "long", "map", "object", "optional",
    "page", "set", "string", "tuple", "void",
}
_TECHNICAL_TOKENS = {
    "adapter", "base", "client", "config", "configuration", "controller", "dao",
    "dto", "handler", "impl", "mapper", "middleware", "model", "po", "repository",
    "request", "response", "service", "test", "util", "utils", "vo", "worker",
}
_TECHNICAL_RESOURCES = {"admin", "docs", "health", "metrics", "openapi", "swagger"}
_METHOD_ACTIONS = {"POST": "Create", "PUT": "Update", "PATCH": "Update", "DELETE": "Delete", "GET": "Get"}


def _tokens(value: str) -> list[str]:
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", value or "")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return [item.lower() for item in re.findall(r"[A-Za-z\u0080-\uffff]+|\d+", value)]


def _singular(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("ses"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def normalize_term(value: str) -> str:
    parts = [_singular(item) for item in _tokens(value)]
    while parts and parts[-1] in _TECHNICAL_TOKENS:
        parts.pop()
    return "_".join(parts)


def display_name(value: str) -> str:
    parts = [
        _singular(item) for item in _tokens(value)
        if item not in _GENERIC_TYPE_NAMES and item not in _TECHNICAL_TOKENS
    ]
    return "".join(item[:1].upper() + item[1:] for item in parts)


def _model_names(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        yield from _model_names(value.get("type"))
        yield from _model_names(value.get("model"))
        yield from _model_names(value.get("name"))
        return
    if not isinstance(value, str):
        return
    for item in re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", value):
        if item.lower() not in _GENERIC_TYPE_NAMES:
            yield item


def _path_resource(path: str) -> str:
    for part in (path or "").strip("/").split("/"):
        clean = part.strip("{}")
        if not clean or clean.startswith(":") or clean.lower() in {"api", "v1", "v2", "v3", "v4"}:
            continue
        return _singular(clean)
    return ""


def _stable_id(repository_id: str, bounded_context: str, term_type: str, normalized: str) -> str:
    raw = "|".join((repository_id, bounded_context, term_type, normalized))
    return "term-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class TermRecognizer:
    """Generate ranked candidates from API and core-entity facts."""

    def extract(
        self,
        snapshot_id: str,
        repository_id: str,
        facts: dict[str, Any],
        term_types: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        selected = set(term_types or {"resource", "entity", "action"})
        bounded_context = repository_id
        buckets: dict[tuple[str, str], TermCandidate] = {}
        entity_by_normalized: dict[str, TermCandidate] = {}
        endpoints = ((facts.get("api_contract") or {}).get("endpoints") or [])
        entities = facts.get("core_entities") or []

        for item in entities:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            candidate = self._candidate(
                buckets, repository_id, snapshot_id, bounded_context,
                "entity", str(item["name"]), selected,
            )
            if candidate is None:
                continue
            candidate.domain_score = min(1.0, max(0.0, float(item.get("score") or 0) / 40.0))
            candidate.mentions.append({
                "node_id": item.get("node_id"), "symbol_name": item.get("name"),
                "qualified_name": item.get("qualified_name"), "file_path": item.get("file_path"),
                "start_line": item.get("start_line", 0), "mention_kind": "entity_definition",
            })
            candidate.evidence.append(TermEvidence(
                "entity_definition", str(item.get("kind") or "type"), 0.42,
                {"file_path": item.get("file_path", ""), "line": item.get("start_line", 0)},
                "entity_definition", item.get("node_id"),
            ))
            fields = item.get("fields") or []
            if fields:
                candidate.evidence.append(TermEvidence(
                    "field_structure", f"{len(fields)} fields", 0.12, {}, "entity_fields", item.get("node_id")
                ))
            if item.get("relationship_count"):
                candidate.evidence.append(TermEvidence(
                    "type_relationship", f"{item['relationship_count']} relationships", 0.08, {}, "entity_relationships", item.get("node_id")
                ))
            annotations = item.get("annotations") or []
            if annotations:
                candidate.evidence.append(TermEvidence(
                    "annotation", ", ".join(map(str, annotations)), 0.14, {}, "entity_annotation", item.get("node_id")
                ))
            entity_by_normalized[normalize_term(str(item["name"]))] = candidate

        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            method = str(endpoint.get("method") or "").upper()
            path = str(endpoint.get("path") or "/")
            resource = _path_resource(path)
            if resource and "resource" in selected and resource.lower() not in _TECHNICAL_RESOURCES:
                candidate = self._candidate(
                    buckets, repository_id, snapshot_id, bounded_context,
                    "resource", resource, selected,
                )
                if candidate:
                    candidate.mentions.append({
                        "node_id": endpoint.get("node_id"), "symbol_name": path,
                        "qualified_name": endpoint.get("handler", ""),
                        "file_path": endpoint.get("file", ""), "start_line": endpoint.get("line", 0),
                        "mention_kind": "api_route",
                    })
                    candidate.evidence.append(TermEvidence(
                        "api_route", f"{method} {path}", 0.48,
                        {"file_path": endpoint.get("file", ""), "line": endpoint.get("line", 0)},
                        "explicit_api_route", endpoint.get("node_id"),
                    ))
                    if endpoint.get("request_body") or endpoint.get("response_body") or endpoint.get("return_type"):
                        candidate.evidence.append(TermEvidence(
                            "api_schema", "typed request/response contract", 0.18, {}, "api_schema", endpoint.get("node_id")
                        ))

            if "action" in selected:
                handler = str(endpoint.get("handler") or "").split("::")[-1].split(".")[-1]
                action = display_name(handler) if handler else ""
                if not action and resource and method in _METHOD_ACTIONS:
                    action = _METHOD_ACTIONS[method] + display_name(resource)
                if action and not self._is_technical(action):
                    candidate = self._candidate(
                        buckets, repository_id, snapshot_id, bounded_context,
                        "action", action, selected,
                    )
                    if candidate:
                        candidate.mentions.append({
                            "node_id": endpoint.get("node_id"), "symbol_name": handler,
                            "qualified_name": endpoint.get("handler", ""),
                            "file_path": endpoint.get("file", ""), "start_line": endpoint.get("line", 0),
                            "mention_kind": "api_handler",
                        })
                        candidate.evidence.append(TermEvidence(
                            "api_handler", handler or f"{method} {path}", 0.42,
                            {"file_path": endpoint.get("file", ""), "line": endpoint.get("line", 0)},
                            "api_handler", endpoint.get("node_id"),
                        ))
                        candidate.evidence.append(TermEvidence(
                            "api_method", method, 0.18, {}, "api_method", endpoint.get("node_id")
                        ))
                        candidate.evidence.append(TermEvidence(
                            "api_route", f"{method} {path}", 0.25,
                            {"file_path": endpoint.get("file", ""), "line": endpoint.get("line", 0)},
                            "action_api_route", endpoint.get("node_id"),
                        ))

            for model in self._endpoint_models(endpoint):
                entity = entity_by_normalized.get(normalize_term(model))
                if entity is not None:
                    resource_candidate = buckets.get(("resource", normalize_term(resource)))
                    if resource_candidate is not None:
                        resource_candidate.evidence.append(TermEvidence(
                            "api_entity_link", f"{method} {path} → {model}", 0.20,
                            {"file_path": endpoint.get("file", ""), "line": endpoint.get("line", 0)},
                            "api_entity_link", endpoint.get("node_id"),
                        ))
                    entity.evidence.append(TermEvidence(
                        "api_model_reference", f"{method} {path} → {model}", 0.20,
                        {"file_path": endpoint.get("file", ""), "line": endpoint.get("line", 0)},
                        "api_entity_link", endpoint.get("node_id"),
                    ))
                    entity.aliases.append(model)

        all_candidates = []
        for candidate in buckets.values():
            for index, evidence in enumerate(candidate.evidence):
                evidence.id = "ev-" + hashlib.sha256(
                    f"{candidate.id}|{index}|{evidence.evidence_type}|{evidence.evidence_value}".encode("utf-8")
                ).hexdigest()[:24]
            self._finalize(candidate)
            all_candidates.append(candidate)
        all_candidates.sort(key=lambda item: (-item.confidence_score, item.term_type, item.id))

        serialized = [item.to_dict() | {"rank": index} for index, item in enumerate(all_candidates, 1)]
        default_terms = [item for item in serialized if item["status"] == "accepted"]
        candidates = [item for item in serialized if item["status"] != "accepted"]
        relations = self._resource_entity_relations(all_candidates)
        return {
            "schema_version": "terms/v1",
            "snapshot_id": snapshot_id,
            "repository_id": repository_id,
            "default_terms": default_terms,
            "candidates": candidates,
            "terms": serialized,
            "relations": [item.to_dict() for item in relations],
            "counts": {
                "total": len(serialized), "accepted": len(default_terms),
                "candidates": len(candidates), "needs_review": sum(item["status"] == "needs_review" for item in candidates),
            },
        }

    @staticmethod
    def _endpoint_models(endpoint: dict[str, Any]) -> list[str]:
        values = [endpoint.get("request_body"), endpoint.get("response_body"), endpoint.get("return_type")]
        return list(dict.fromkeys(model for value in values for model in _model_names(value)))

    @staticmethod
    def _is_technical(name: str) -> bool:
        return any(token in _TECHNICAL_TOKENS for token in _tokens(name))

    def _candidate(
        self, buckets: dict[tuple[str, str], TermCandidate], repository_id: str,
        snapshot_id: str, bounded_context: str, term_type: str, raw_name: str,
        selected: set[str],
    ) -> TermCandidate | None:
        if term_type not in selected:
            return None
        normalized = normalize_term(raw_name)
        canonical = display_name(raw_name)
        if not normalized or not canonical:
            return None
        key = (term_type, normalized)
        if key not in buckets:
            buckets[key] = TermCandidate(
                id=_stable_id(repository_id, bounded_context, term_type, normalized),
                repository_id=repository_id, snapshot_id=snapshot_id,
                canonical_name=canonical, normalized_name=normalized,
                term_type=term_type, bounded_context=bounded_context,
            )
        if self._is_technical(raw_name):
            buckets[key].risk_flags.append("technical_source")
        else:
            buckets[key].risk_flags = [flag for flag in buckets[key].risk_flags if flag != "technical_source"]
        return buckets[key]

    def _finalize(self, candidate: TermCandidate) -> None:
        evidence_types = {item.evidence_type for item in candidate.evidence}
        candidate.confidence_score = min(
            1.0,
            max(0.0, sum(item.weight for item in candidate.evidence) + min(candidate.domain_score * 0.12, 0.12)),
        )
        technical = "technical_source" in candidate.risk_flags
        has_anchor = bool(evidence_types & {"api_route", "entity_definition", "api_handler"})
        has_corroboration = len(evidence_types - {"api_route", "entity_definition", "api_handler"}) > 0
        if technical:
            candidate.status = "rejected"
            candidate.risk_flags.append("technical_name")
        elif candidate.confidence_score >= 0.85 and has_anchor and has_corroboration:
            candidate.status = "accepted"
            candidate.confidence_band = "high"
        elif candidate.confidence_score >= 0.60:
            candidate.status = "needs_review"
            candidate.confidence_band = "medium"
        else:
            candidate.status = "candidate"
            candidate.confidence_band = "low"
        if not has_corroboration:
            candidate.risk_flags.append("single_evidence_group")
        if candidate.term_type == "action" and not any(item.evidence_type == "api_route" for item in candidate.evidence):
            candidate.risk_flags.append("inferred_action")

    @staticmethod
    def _resource_entity_relations(candidates: list[TermCandidate]) -> list[TermRelation]:
        resources = {item.normalized_name: item for item in candidates if item.term_type == "resource"}
        entities = {item.normalized_name: item for item in candidates if item.term_type == "entity"}
        relations = []
        for normalized, resource in resources.items():
            entity = entities.get(normalized)
            if entity is None:
                continue
            relations.append(TermRelation(
                resource.id, entity.id, "exposes", min(resource.confidence_score, entity.confidence_score),
                "accepted" if resource.status == entity.status == "accepted" else "candidate",
                {"name_similarity": 1.0, "api_entity_evidence": 1.0},
            ))
        return relations
