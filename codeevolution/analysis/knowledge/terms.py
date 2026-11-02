"""Deterministic, evidence-backed terminology candidate extraction.

This module deliberately consumes frozen knowledge facts instead of reading a
live checkout.  It does not ask an LLM to invent terms: the LLM integration,
when added, can only adjudicate candidates produced here.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any, Iterable

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
    TERM_TECHNICAL_COMPONENT,
    TERM_TYPES,
    canonical_term_type,
)
from ...domain.terms import TermCandidate, TermEvidence, TermRelation
from .evidence import decision_band, fuse, term_confidence

TERM_ANALYSIS_VERSION = "terms-v3"

_GENERIC_TYPE_NAMES = {
    "any", "bool", "boolean", "commonpage", "commonresult", "dict", "double",
    "float", "int", "integer", "list", "long", "map", "object", "optional",
    "page", "set", "string", "tuple", "void",
}
_TECHNICAL_TOKENS = {
    "adapter", "api", "base", "client", "config", "configuration", "controller",
    "dao", "dto", "handler", "impl", "mapper", "middleware", "model", "po",
    "repository", "request", "response", "service", "snapshot", "test", "util",
    "utils", "vo", "worker",
    # frontend chrome and framework plumbing around pages, not business terms
    "layout", "breadcrumb", "sidebar", "navbar", "header", "footer", "menu",
    "router", "prop", "props", "emit", "emits",
}
_TECHNICAL_RESOURCES = {"admin", "docs", "health", "metrics", "openapi", "swagger"}
# Generic data-operation verbs: names carrying these prefixes are operations on
# terms (e.g. GetUserList, CreateOrder), not terms themselves.  CRUD-led names
# merge onto the noun they operate on; control/compute verbs reject outright.
_CRUD_VERBS = {
    "get", "query", "list", "find", "search", "fetch", "load", "read", "scan",
    "add", "create", "insert", "save", "store", "register", "new", "set", "put",
    "update", "modify", "edit", "change", "batch", "bulk",
    "delete", "remove", "del", "drop", "page", "count", "exists", "check",
}
# Control/compute verbs: compounds led or tailed by one of these (ViewReport,
# GraphView, CancelAnalysisRun, TaskRun) are operations on terms or UI
# artifacts, never terms — the candidate is dropped instead of merged.
_OPERATION_VERBS = {
    "view", "preview", "show", "display", "render", "open", "close", "toggle",
    "call", "invoke", "execute", "run", "start", "stop", "cancel", "pause",
    "resume", "abort", "retry", "restart", "kill", "terminate",
    "refresh", "reload", "pin", "unpin", "export", "import", "upload",
    "download", "sync", "enable", "disable", "use",
    "generate", "collect", "scavenge", "serve", "retire", "deploy",
    "analyze", "analyse", "analysis", "compute", "calculate", "parse",
    "resolve", "validate", "verify", "compare", "align", "process",
    "handle", "perform", "apply", "trigger", "submit", "dispatch", "do",
}
# Abstract structural vocabulary: too generic to be a domain term on its own or
# as the head/tail of a compound (File, Entity, FileName, Location).
_ABSTRACT_NOUNS = {
    "file", "path", "folder", "directory", "node", "item", "entry", "value",
    "field", "name", "type", "kind", "entity", "object", "target", "source",
    "dest", "destination", "location", "context", "option", "options", "flag",
    "key", "error", "record", "content", "detail", "details", "summary",
    "manager", "helper", "common", "main", "core", "global", "default",
    "spec", "meta", "misc", "temp", "tmp",
}
# When the same normalized name is extracted as several term types, keep one
# entry: domain objects outrank their API resources and actions.
_TYPE_PRIORITY = {
    TERM_ENTITY: 0, TERM_BUSINESS_EVENT: 1, TERM_BUSINESS_ENUM: 2,
    TERM_BUSINESS_ATTRIBUTE: 3, TERM_COMMAND: 4, TERM_BUSINESS_ACTION: 5,
    TERM_API_RESOURCE: 6, TERM_DTO: 7, TERM_PAGE: 8, TERM_TECHNICAL_COMPONENT: 9,
}
# Generic query/view nouns that carry no domain meaning when appended to a term.
_GENERIC_NOUNS = {
    "list", "data", "info", "page", "all", "by", "result", "results", "param",
    "params", "parameter", "parameters", "filter", "filters", "criteria",
    "condition", "conditions", "query",
}
# A small set of fields (id/status/amount) is shared by unrelated concepts;
# only compound, domain-specific fields become attribute terms.
_GENERIC_ATTRIBUTE_TOKENS = {
    "id", "name", "type", "code", "value", "count", "date", "time",
    "created", "updated", "deleted", "at", "by", "on", "of", "to", "from",
}
# Only these type collisions mean "same name, different concept".  Page, DTO
# and API resource are representation roles of the entity they normalize to.
_HARD_TYPE_CONFLICTS = {
    frozenset({TERM_ENTITY, TERM_BUSINESS_EVENT}),
    frozenset({TERM_ENTITY, TERM_BUSINESS_ACTION}),
    frozenset({TERM_ENTITY, TERM_COMMAND}),
    frozenset({TERM_ENTITY, TERM_BUSINESS_ENUM}),
    frozenset({TERM_BUSINESS_EVENT, TERM_COMMAND}),
}
_P0_EVIDENCE = {"api_route", "entity_definition", "db_table", "frontend_page"}
_P1_EVIDENCE = {
    "enum_definition", "message_contract", "command_definition",
    "event_definition", "dto_definition", "service_method",
    "attribute_definition", "db_column", "structural_type",
}


def _tokens(value: str) -> list[str]:
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", value or "")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return [item.lower() for item in re.findall(r"[A-Za-z\u0080-\uffff]+|\d+", value)]


def _singular(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    # -sis nouns (analysis, basis, ...) are already singular.
    if token.endswith("sis"):
        return token
    if len(token) > 3 and token.endswith("ses"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us")):
        return token[:-1]
    return token


def _domain_parts(parts: list[str]) -> list[str]:
    """Strip operation ingredients: trailing technical wrappers, leading CRUD
    verbs and trailing generic query nouns leave the domain noun phrase."""
    while parts and parts[-1] in _TECHNICAL_TOKENS:
        parts.pop()
    while parts and parts[0] in _CRUD_VERBS:
        parts.pop(0)
    while parts and parts[-1] in _GENERIC_NOUNS:
        parts.pop()
    return parts


def normalize_term(value: str) -> str:
    return "_".join(_domain_parts([_singular(item) for item in _tokens(value)]))


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


def _module_for(file_path: str, modules: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_length = -1
    for module in modules:
        files = module.get("files") or []
        paths = [str(item) for item in files] + [str(module.get("path") or "")]
        for path in paths:
            if path and (file_path == path or file_path.startswith(path.rstrip("/") + "/")):
                if len(path) > best_length:
                    best, best_length = module, len(path)
    return best


_EXTENSION_LANGUAGES = {
    ".py": "python", ".java": "java", ".kt": "kotlin", ".go": "go",
    ".rs": "rust", ".cs": "csharp", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".vue": "vue", ".sql": "sql",
}


def _language_for(file_path: str) -> str:
    for suffix, language in _EXTENSION_LANGUAGES.items():
        if file_path.endswith(suffix):
            return language
    return ""



class TermRecognizer:
    """Generate ranked candidates from API and core-entity facts."""

    def extract(
        self,
        snapshot_id: str,
        repository_id: str,
        facts: dict[str, Any],
        term_types: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        selected = {canonical_term_type(item) for item in (term_types or TERM_TYPES)}
        selected &= set(TERM_TYPES)
        bounded_context = repository_id
        buckets: dict[tuple[str, str], TermCandidate] = {}
        clean_names: dict[tuple[str, str], bool] = {}
        entity_by_normalized: dict[str, TermCandidate] = {}
        endpoints = ((facts.get("api_contract") or {}).get("endpoints") or [])
        entities = facts.get("core_entities") or []
        pages = ((facts.get("frontend_pages") or {}).get("pages") or [])
        term_facts = facts.get("term_facts") or {}
        modules = (facts.get("module_topology") or {}).get("modules") or []
        attribute_links: list[tuple[str, str]] = []

        # Frontend pages (router entries, views/ or pages/ components) are
        # business surfaces: their names become page terms and the route
        # meta title is kept as a human-readable alias.
        for page in pages:
            if not isinstance(page, dict) or not page.get("name"):
                continue
            candidate = self._candidate(
                buckets, repository_id, snapshot_id, bounded_context,
                TERM_PAGE, str(page["name"]), selected, clean_names,
            )
            if candidate is None:
                continue
            if page.get("title"):
                candidate.aliases.append(str(page["title"]))
            candidate.mentions.append({
                "node_id": None, "symbol_name": page["name"],
                "qualified_name": page.get("route") or page["name"],
                "file_path": page.get("file", ""), "start_line": 0,
                "mention_kind": "frontend_page",
            })
            candidate.evidence.append(TermEvidence(
                "frontend_page", str(page.get("route") or page["name"]), 0.45,
                {"file_path": page.get("file", ""), "line": 0},
                "frontend_page",
            ))
            if page.get("title"):
                candidate.evidence.append(TermEvidence(
                    "page_title", str(page["title"]), 0.18, {}, "frontend_page_title",
                ))

        for item in entities:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            raw_name = str(item["name"])
            candidate = self._candidate(
                buckets, repository_id, snapshot_id, bounded_context,
                TERM_ENTITY, raw_name, selected, clean_names,
            )
            if candidate is None:
                continue
            if raw_name != candidate.canonical_name:
                candidate.aliases.append(raw_name)
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
            for field in fields:
                field_name = field.get("name") if isinstance(field, dict) else str(field)
                if not self._is_business_attribute(field_name):
                    continue
                attribute = self._candidate(
                    buckets, repository_id, snapshot_id, bounded_context,
                    TERM_BUSINESS_ATTRIBUTE, field_name, selected, clean_names,
                )
                if attribute is None:
                    continue
                line = field.get("start_line", 0) if isinstance(field, dict) else 0
                attribute.mentions.append({
                    "node_id": item.get("node_id"), "symbol_name": field_name,
                    "qualified_name": item.get("qualified_name"),
                    "file_path": item.get("file_path"), "start_line": line,
                    "mention_kind": "attribute_definition",
                })
                attribute.evidence.append(TermEvidence(
                    "attribute_definition", field_name, 0.30,
                    {"file_path": item.get("file_path", ""), "line": line},
                    "attribute_definition", item.get("node_id"),
                ))
                attribute_links.append((candidate.normalized_name, attribute.normalized_name))
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

        action_sources: list[tuple[str, dict[str, Any]]] = []
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            method = str(endpoint.get("method") or "").upper()
            path = str(endpoint.get("path") or "/")
            resource = _path_resource(path)
            if resource and TERM_API_RESOURCE in selected and resource.lower() not in _TECHNICAL_RESOURCES:
                candidate = self._candidate(
                    buckets, repository_id, snapshot_id, bounded_context,
                    TERM_API_RESOURCE, resource, selected, clean_names,
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

            if TERM_BUSINESS_ACTION in selected:
                handler = str(endpoint.get("handler") or "").split("::")[-1].split(".")[-1]
                if handler:
                    action_sources.append((handler, endpoint))

            for model in self._endpoint_models(endpoint):
                entity = entity_by_normalized.get(normalize_term(model))
                if entity is not None:
                    resource_candidate = buckets.get((TERM_API_RESOURCE, normalize_term(resource)))
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

        # Data operations (GetUserList / CreateOrder / QueryUser) are operations
        # on terms rather than terms: only handlers led by a domain verb become
        # action terms, and each is linked back to the noun it operates on.
        noun_keys = set(entity_by_normalized)
        noun_keys.update(normalized for term_type, normalized in buckets if term_type == TERM_API_RESOURCE)
        applies_to: list[tuple[TermCandidate, str]] = []
        for handler, endpoint in action_sources:
            tokens = _tokens(handler)
            if (
                not tokens or tokens[0] in _CRUD_VERBS or tokens[0] in _GENERIC_NOUNS
                or tokens[0] in _OPERATION_VERBS or tokens[-1] in _OPERATION_VERBS
                or tokens[0] in _ABSTRACT_NOUNS or tokens[-1] in _ABSTRACT_NOUNS
                or self._is_technical(handler)
            ):
                continue
            if normalize_term(handler) in noun_keys:
                continue
            candidate = self._candidate(
                buckets, repository_id, snapshot_id, bounded_context,
                TERM_BUSINESS_ACTION, handler, selected, clean_names,
            )
            if candidate is None:
                continue
            candidate.mentions.append({
                "node_id": endpoint.get("node_id"), "symbol_name": handler,
                "qualified_name": endpoint.get("handler", ""),
                "file_path": endpoint.get("file", ""), "start_line": endpoint.get("line", 0),
                "mention_kind": "api_handler",
            })
            candidate.evidence.append(TermEvidence(
                "api_handler", handler, 0.42,
                {"file_path": endpoint.get("file", ""), "line": endpoint.get("line", 0)},
                "api_handler", endpoint.get("node_id"),
            ))
            candidate.evidence.append(TermEvidence(
                "api_method", str(endpoint.get("method") or "").upper(), 0.18, {}, "api_method", endpoint.get("node_id")
            ))
            candidate.evidence.append(TermEvidence(
                "api_route", f"{str(endpoint.get('method') or '').upper()} {endpoint.get('path') or '/'}", 0.25,
                {"file_path": endpoint.get("file", ""), "line": endpoint.get("line", 0)},
                "action_api_route", endpoint.get("node_id"),
            ))
            residue = [_singular(token) for token in tokens[1:]]
            while residue and residue[-1] in _GENERIC_NOUNS:
                residue.pop()
            if residue:
                applies_to.append((candidate, "_".join(residue)))

        self._mine_term_facts(
            term_facts, buckets, repository_id, snapshot_id, bounded_context,
            selected, clean_names, entity_by_normalized, attribute_links,
        )

        # The same name often surfaces as both an API resource and a domain
        # entity (or an action): deduplicate into one entry per normalized
        # name, keeping the strongest type and merging all its evidence.
        conflicts: list[dict[str, Any]] = []

        def merge(grouping: dict[str, list[TermCandidate]]) -> list[TermCandidate]:
            merged: list[TermCandidate] = []
            for entries in grouping.values():
                entries.sort(key=lambda item: _TYPE_PRIORITY.get(item.term_type, 9))
                primary = entries[0]
                type_set = {item.term_type for item in entries}
                if any(left in type_set and right in type_set for left, right in _HARD_TYPE_CONFLICTS):
                    primary.risk_flags.append("type_conflict")
                    conflicts.append({
                        "id": "cf-" + hashlib.sha256(
                            f"{snapshot_id}|{primary.id}|same_name_multiple_types|{sorted(type_set)}".encode()
                        ).hexdigest()[:24],
                        "term_id": primary.id,
                        "conflict_type": "same_name_multiple_types",
                        "severity": "review",
                        "detail": {
                            "canonical_name": primary.canonical_name,
                            "types": sorted(type_set),
                        },
                    })
                any_clean = False
                for other in entries:
                    any_clean = any_clean or clean_names.get((other.term_type, other.normalized_name), False)
                    if other is primary:
                        continue
                    primary.mentions.extend(other.mentions)
                    primary.evidence.extend(other.evidence)
                    primary.aliases.extend(other.aliases)
                    primary.domain_score = max(primary.domain_score, other.domain_score)
                if any_clean:
                    primary.risk_flags = [flag for flag in primary.risk_flags if flag != "technical_source"]
                merged.append(primary)
            return merged

        by_normalized: dict[str, list[TermCandidate]] = {}
        for candidate in buckets.values():
            by_normalized.setdefault(candidate.normalized_name, []).append(candidate)
        # A second pass by display name catches wrappers that normalize
        # differently but collapse onto the same visible term
        # (ApiExplanation vs ExplanationSnapshot -> Explanation).
        by_canonical: dict[str, list[TermCandidate]] = {}
        for candidate in merge(by_normalized):
            by_canonical.setdefault(candidate.canonical_name, []).append(candidate)
        # A term without any evidence has no traceable source: drop it.
        all_candidates = [item for item in merge(by_canonical) if item.evidence]
        for candidate in all_candidates:
            for index, evidence in enumerate(candidate.evidence):
                # Evidence ids are the primary key of the snapshot-scoped
                # term_evidence table, so the snapshot must be part of the hash:
                # term ids are stable per repository and repeat across snapshots.
                evidence.id = "ev-" + hashlib.sha256(
                    f"{snapshot_id}|{candidate.id}|{index}|{evidence.evidence_type}|{evidence.evidence_value}".encode("utf-8")
                ).hexdigest()[:24]
            self._finalize(candidate, modules)
        all_candidates.sort(key=lambda item: (-item.confidence_score, item.term_type, item.id))

        serialized = [item.to_dict() | {"rank": index} for index, item in enumerate(all_candidates, 1)]
        default_terms = [item for item in serialized if item["status"] == "accepted"]
        candidates = [item for item in serialized if item["status"] != "accepted"]
        relations = self._resource_entity_relations(all_candidates, applies_to)
        relations.extend(self._attribute_relations(all_candidates, attribute_links))
        priority_counts = Counter(item.priority for item in all_candidates)
        p0 = [item for item in all_candidates if item.priority == "P0"]
        p0_traceable = [item for item in p0 if item.source_refs]
        return {
            "schema_version": "terms/v2",
            "analysis_version": TERM_ANALYSIS_VERSION,
            "snapshot_id": snapshot_id,
            "repository_id": repository_id,
            "default_terms": default_terms,
            "candidates": candidates,
            "terms": serialized,
            "relations": [item.to_dict() for item in relations],
            "asset_inventory": self._asset_inventory(repository_id, snapshot_id, term_facts, modules, endpoints),
            "conflicts": conflicts,
            "traceability": {
                "p0_total": len(p0),
                "p0_traceable": len(p0_traceable),
                "rate": round(len(p0_traceable) / len(p0), 4) if p0 else 1.0,
            },
            "counts": {
                "total": len(serialized), "accepted": len(default_terms),
                "candidates": len(candidates), "needs_review": sum(item["status"] == "needs_review" for item in candidates),
                "priorities": {key: priority_counts.get(key, 0) for key in ("P0", "P1", "P2")},
            },
        }

    @staticmethod
    def _endpoint_models(endpoint: dict[str, Any]) -> list[str]:
        values = [endpoint.get("request_body"), endpoint.get("response_body"), endpoint.get("return_type")]
        return list(dict.fromkeys(model for value in values for model in _model_names(value)))

    def _mine_term_facts(
        self, term_facts: dict[str, Any], buckets, repository_id: str, snapshot_id: str,
        bounded_context: str, selected: set[str], clean_names, entity_by_normalized,
        attribute_links: list[tuple[str, str]],
    ) -> None:
        def add(
            term_type: str, raw_name: str, evidence_type: str, weight: float, rule_id: str,
            node_id: str | None, file_path: str, line: int, mention_kind: str,
            alias: str = "", qualified_name: str = "",
        ) -> TermCandidate | None:
            candidate = self._candidate(
                buckets, repository_id, snapshot_id, bounded_context,
                term_type, raw_name, selected, clean_names,
            )
            if candidate is None:
                return None
            if alias and alias != candidate.canonical_name:
                candidate.aliases.append(alias)
            candidate.mentions.append({
                "node_id": node_id, "symbol_name": raw_name,
                "qualified_name": qualified_name or raw_name,
                "file_path": file_path, "start_line": line, "mention_kind": mention_kind,
            })
            candidate.evidence.append(TermEvidence(
                evidence_type, raw_name, weight,
                {"file_path": file_path, "line": line}, rule_id, node_id,
            ))
            return candidate

        for item in term_facts.get("enums") or []:
            candidate = add(
                TERM_BUSINESS_ENUM, item.get("name", ""), "enum_definition", 0.45,
                "enum_definition", item.get("node_id"), item.get("file_path", ""),
                int(item.get("start_line") or 0), "enum_definition",
                qualified_name=item.get("qualified_name", ""),
            )
            members = [str(member) for member in item.get("members") or []]
            if candidate is not None and members:
                candidate.evidence.append(TermEvidence(
                    "enum_members", ", ".join(members[:8]), 0.12, {}, "enum_members", item.get("node_id"),
                ))

        type_specs = {
            "event": (TERM_BUSINESS_EVENT, "event_definition", 0.45),
            "command": (TERM_COMMAND, "command_definition", 0.42),
            "dto": (TERM_DTO, "dto_definition", 0.34),
            "entity": (TERM_ENTITY, "structural_type", 0.22),
        }
        for item in term_facts.get("types") or []:
            spec = type_specs.get(str(item.get("classification") or ""))
            if spec is None:
                continue
            raw_name = str(item.get("name") or "")
            normalized = normalize_term(raw_name)
            if item.get("classification") == "entity" and normalized in entity_by_normalized:
                continue
            # A command must keep its verb (CreateOrderCommand is a command on
            # Order); events and DTOs take the stripped domain noun.
            term_name = raw_name if item.get("classification") == "command" else str(item.get("base_name") or raw_name)
            add(
                spec[0], term_name, spec[1], spec[2],
                spec[1], item.get("node_id"), item.get("file_path", ""),
                int(item.get("start_line") or 0), spec[1], alias=raw_name,
                qualified_name=item.get("qualified_name", ""),
            )

        for item in term_facts.get("db_tables") or []:
            table = add(
                TERM_ENTITY, item.get("name", ""), "db_table", 0.50,
                "db_table", None, item.get("file_path", ""),
                int(item.get("start_line") or 0), "db_table",
            )
            if table is None:
                continue
            for key in item.get("primary_key") or []:
                table.evidence.append(TermEvidence(
                    "identity_key", str(key), 0.12, {}, "primary_key", None,
                ))
            for column in item.get("columns") or []:
                if not self._is_business_attribute(str(column)):
                    continue
                attribute = add(
                    TERM_BUSINESS_ATTRIBUTE, str(column), "db_column", 0.35,
                    "db_column", None, item.get("file_path", ""),
                    int(item.get("start_line") or 0), "db_column",
                )
                if attribute is not None:
                    attribute_links.append((table.normalized_name, attribute.normalized_name))

        for item in term_facts.get("messages") or []:
            candidate = add(
                TERM_BUSINESS_EVENT, item.get("topic", ""), "message_contract", 0.50,
                "message_contract", item.get("node_id"), item.get("file_path", ""),
                int(item.get("start_line") or 0), "message_contract",
                alias=str(item.get("symbol") or ""),
            )
            if candidate is not None:
                candidate.evidence.append(TermEvidence(
                    "message_role", str(item.get("role") or ""), 0.10, {},
                    "message_contract", item.get("node_id"),
                ))

        for item in term_facts.get("service_methods") or []:
            add(
                TERM_BUSINESS_ACTION, item.get("name", ""), "service_method", 0.34,
                "service_method", None, item.get("file_path", ""),
                int(item.get("start_line") or 0), "service_method",
                qualified_name=item.get("qualified_name", ""),
            )

    @staticmethod
    def _is_business_attribute(name: str) -> bool:
        tokens = _tokens(name)
        if len(tokens) < 2 or tokens[-1] == "id":
            return False
        return not any(
            token in _GENERIC_ATTRIBUTE_TOKENS or token in _ABSTRACT_NOUNS for token in tokens
        )

    @staticmethod
    def _source_refs(candidate: TermCandidate) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        for mention in candidate.mentions:
            path = str(mention.get("file_path") or "")
            if not path:
                continue
            ref = {
                "node_id": mention.get("node_id") or "",
                "symbol": mention.get("symbol_name") or "",
                "qualified_name": mention.get("qualified_name") or "",
                "file_path": path,
                "line": int(mention.get("start_line") or 0),
                "kind": mention.get("mention_kind") or "mention",
            }
            key = (ref["node_id"], ref["file_path"], ref["line"], ref["kind"])
            if key not in seen:
                seen.add(key)
                refs.append(ref)
        if refs:
            return refs
        for evidence in candidate.evidence:
            location = evidence.source_location or {}
            path = str(location.get("file_path") or "")
            if not path:
                continue
            ref = {
                "node_id": evidence.node_id or "", "symbol": "",
                "qualified_name": "", "file_path": path,
                "line": int(location.get("line") or 0), "kind": evidence.evidence_type,
            }
            key = (ref["node_id"], ref["file_path"], ref["line"], ref["kind"])
            if key not in seen:
                seen.add(key)
                refs.append(ref)
        return refs

    @staticmethod
    def _asset_inventory(
        repository_id: str, snapshot_id: str, term_facts: dict[str, Any],
        modules: list[dict[str, Any]], endpoints: list[dict[str, Any]],
    ) -> dict[str, Any]:
        assets = [
            {"asset_type": "service", "name": repository_id,
             "locator": {"repository_id": repository_id}, "source_version": snapshot_id},
            {"asset_type": "repository", "name": repository_id,
             "locator": {"repository_id": repository_id}, "source_version": snapshot_id},
        ]
        for module in modules:
            assets.append({
                "asset_type": "module", "name": str(module.get("id") or module.get("name") or ""),
                "locator": {"path": module.get("path", ""), "file_count": module.get("file_count", 0)},
                "source_version": snapshot_id,
            })
        for item in term_facts.get("languages") or []:
            assets.append({
                "asset_type": "language", "name": str(item.get("language") or ""),
                "locator": {"file_count": item.get("file_count", 0)}, "source_version": snapshot_id,
            })
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            assets.append({
                "asset_type": "api_endpoint",
                "name": f"{str(endpoint.get('method') or '').upper()} {endpoint.get('path') or '/'}",
                "locator": {"file_path": endpoint.get("file", ""), "line": endpoint.get("line", 0)},
                "source_version": snapshot_id,
            })
        for key, asset_type, name_key in (
            ("db_tables", "db_table", "name"),
            ("messages", "message_topic", "topic"),
            ("enums", "enum", "name"),
            ("types", "type", "name"),
        ):
            for item in term_facts.get(key) or []:
                assets.append({
                    "asset_type": asset_type, "name": str(item.get(name_key) or ""),
                    "locator": {"file_path": item.get("file_path", ""), "line": item.get("start_line", 0)},
                    "source_version": snapshot_id,
                })
        counts = Counter(item["asset_type"] for item in assets)
        return {"counts": {**dict(counts), "total": len(assets)}, "assets": assets}

    @staticmethod
    def _attribute_relations(
        candidates: list[TermCandidate], links: list[tuple[str, str]]
    ) -> list[TermRelation]:
        by_key = {(item.term_type, item.normalized_name): item for item in candidates}
        relations: list[TermRelation] = []
        seen: set[tuple[str, str]] = set()
        for parent_key, attribute_key in links:
            parent = by_key.get((TERM_ENTITY, parent_key))
            attribute = by_key.get((TERM_BUSINESS_ATTRIBUTE, attribute_key))
            if parent is None or attribute is None or parent.id == attribute.id:
                continue
            key = (parent.id, attribute.id)
            if key in seen:
                continue
            seen.add(key)
            relations.append(TermRelation(
                parent.id, attribute.id, "has_attribute",
                min(parent.confidence_score, attribute.confidence_score),
                "accepted" if parent.status == attribute.status == "accepted" else "candidate",
                {"field_link": 1.0},
            ))
        return relations

    @staticmethod
    def _is_technical(name: str) -> bool:
        return any(token in _TECHNICAL_TOKENS for token in _tokens(name))

    def _candidate(
        self, buckets: dict[tuple[str, str], TermCandidate], repository_id: str,
        snapshot_id: str, bounded_context: str, term_type: str, raw_name: str,
        selected: set[str], clean_names: dict[tuple[str, str], bool] | None = None,
    ) -> TermCandidate | None:
        if term_type not in selected:
            return None
        tokens = _tokens(raw_name)
        # Operation-verb compounds (ViewReport, GraphView, TaskRun) are
        # operations on terms rather than terms, and abstract structural
        # vocabulary (File, Entity, FileName) is too generic to be one:
        # reject both outright.
        if tokens and (
            tokens[0] in _OPERATION_VERBS or tokens[-1] in _OPERATION_VERBS
            or tokens[0] in _ABSTRACT_NOUNS or tokens[-1] in _ABSTRACT_NOUNS
        ):
            return None
        normalized = normalize_term(raw_name)
        # Canonical name comes from the stripped noun phrase so operation
        # wrappers (e.g. CreateUserRequest) collapse onto the real term.
        canonical = display_name(" ".join(normalized.split("_")))
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
        # A technical wrapper (e.g. CreateUserRequest) must not poison a bucket
        # that has already seen a clean domain name: flag only when every
        # source name for the term is technical.
        clean = not self._is_technical(raw_name)
        if clean_names is not None:
            clean_names[key] = clean_names.get(key, False) or clean
        if clean_names is not None and not clean_names[key]:
            buckets[key].risk_flags.append("technical_source")
        else:
            buckets[key].risk_flags = [flag for flag in buckets[key].risk_flags if flag != "technical_source"]
        return buckets[key]

    def _finalize(self, candidate: TermCandidate, modules: list[dict[str, Any]] | None = None) -> None:
        candidate.evidence = fuse(candidate.term_type, candidate.evidence)
        candidate.source_refs = self._source_refs(candidate)
        file_path = next((ref["file_path"] for ref in candidate.source_refs), "")
        module = _module_for(file_path, modules or []) if file_path else None
        candidate.module = str((module or {}).get("id") or "")
        candidate.language = str((module or {}).get("primary_language") or "") or _language_for(file_path)
        evidence_types = {item.evidence_type for item in candidate.evidence}
        # Source doubles as the evidence-chain summary, e.g. "rule:api_route+entity_definition".
        candidate.source = "rule:" + "+".join(sorted(evidence_types))
        candidate.confidence_score = term_confidence(candidate)
        candidate.term_confidence = candidate.confidence_score
        candidate.decision_band = decision_band(candidate.confidence_score)
        traceable = bool(file_path)
        if traceable and evidence_types & _P0_EVIDENCE:
            candidate.priority = "P0"
        elif traceable and evidence_types & _P1_EVIDENCE:
            candidate.priority = "P1"
        else:
            candidate.priority = "P2"
        technical = "technical_source" in candidate.risk_flags
        has_anchor = bool(
            evidence_types & {"api_route", "entity_definition", "api_handler", "frontend_page", "db_table"}
        )
        has_corroboration = len(
            evidence_types - {"api_route", "entity_definition", "api_handler", "db_table"}
        ) > 0
        if technical:
            candidate.status = "rejected"
            candidate.lifecycle_status = "REJECTED"
            candidate.risk_flags.append("technical_name")
        elif "type_conflict" in candidate.risk_flags:
            candidate.status = "needs_review"
            candidate.confidence_band = "medium"
            candidate.lifecycle_status = "REVIEW_PENDING"
        elif candidate.confidence_score >= 0.85 and has_anchor and has_corroboration:
            candidate.status = "accepted"
            candidate.confidence_band = "high"
            candidate.lifecycle_status = "ANALYZED"
        elif candidate.confidence_score >= 0.60:
            candidate.status = "needs_review"
            candidate.confidence_band = "medium"
            candidate.lifecycle_status = "REVIEW_PENDING"
        else:
            candidate.status = "candidate"
            candidate.confidence_band = "low"
            candidate.lifecycle_status = "DISCOVERED"
        if not has_corroboration:
            candidate.risk_flags.append("single_evidence_group")
        if candidate.term_type == TERM_BUSINESS_ACTION and not (
            evidence_types & {"api_route", "service_method"}
        ):
            candidate.risk_flags.append("inferred_action")

    @staticmethod
    def _resource_entity_relations(
        candidates: list[TermCandidate],
        applies_to: list[tuple[TermCandidate, str]] = (),
    ) -> list[TermRelation]:
        resources = {item.normalized_name: item for item in candidates if item.term_type == TERM_API_RESOURCE}
        entities = {item.normalized_name: item for item in candidates if item.term_type == TERM_ENTITY}
        nouns = {**resources, **entities}
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
        for action, residue_key in applies_to:
            target = nouns.get(residue_key)
            if target is None or target.id == action.id:
                continue
            relations.append(TermRelation(
                action.id, target.id, "applies_to", min(action.confidence_score, target.confidence_score),
                "accepted" if action.status == target.status == "accepted" else "candidate",
                {"operation_residue": 1.0},
            ))
        return relations
