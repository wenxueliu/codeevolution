"""Deterministic interface -> group -> service business-rule map.

Phase 1 of ``design/business-rule-hierarchy.md``: collect the business rules
and integration contracts already published in API explanation snapshots,
normalise them into rule atoms, and roll them up by route resource group and
by service.  No model calls and no writes: the map is a read-only projection
that later phases can extend with interface rules, conflict detection and
cross-service composition.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from typing import Any

from ..analysis.knowledge.api_contract import ApiContractExtractor

RULE_MAP_VERSION = 1

_TRAILING = re.compile(r"[\s。.;；,，、]+$")
_SPACES = re.compile(r"\s+")

CROSS_SERVICE_KINDS = frozenset({"http", "rpc", "message"})
KNOWN_INTEGRATION_KINDS = frozenset({
    "http", "rpc", "message", "database", "cache", "file", "external", "function",
})


def api_key(method: str, path: str, handler: str) -> str:
    return "|".join((str(method or "").upper(), str(path or ""), str(handler or "")))


def normalize_rule_text(text: str) -> str:
    """Stable comparison form; the original text stays untouched for display."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("≥", ">=").replace("≤", "<=").replace("＞", ">").replace("＜", "<")
    value = _SPACES.sub(" ", value).strip()
    return _TRAILING.sub("", value)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _rule_text(rule: Any) -> str:
    if isinstance(rule, str):
        return rule.strip()
    if isinstance(rule, dict):
        for key in ("rule", "text", "description", "description_zh", "description_en"):
            value = rule.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _rule_field(rule: Any, *keys: str) -> str:
    if not isinstance(rule, dict):
        return ""
    for key in keys:
        value = rule.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _rule_lines(rule: Any) -> list[int]:
    if not isinstance(rule, dict):
        return []
    lines = rule.get("evidence_lines")
    if not isinstance(lines, list):
        return []
    return [int(line) for line in lines if isinstance(line, (int, float))]


def _integration_kind(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "external"
    for part in raw.replace("|", ",").replace("/", ",").split(","):
        token = part.strip()
        if token in KNOWN_INTEGRATION_KINDS:
            return token
    return "external"


class RuleMapService:
    """Project interface rules into atoms, groups and a service-level view."""

    def __init__(self, queries, explanation_store=None):
        self.queries = queries
        self.store = explanation_store

    def build(self, snapshot_id: str, *, level: str = "group") -> dict[str, Any]:
        del level  # the projection always carries every level; callers choose what to render
        contract = self.queries.api_contract(snapshot_id)
        endpoints = [item for item in contract.get("endpoints") or [] if isinstance(item, dict)]

        atoms: dict[str, dict[str, Any]] = {}
        interfaces: list[dict[str, Any]] = []
        integrations: list[dict[str, Any]] = []
        for endpoint in endpoints:
            method = str(endpoint.get("method") or "").upper()
            path = str(endpoint.get("path") or "")
            handler = str(endpoint.get("handler") or "")
            key = api_key(method, path, handler)
            group_key = ApiContractExtractor.resource_prefix(path) or "root"
            aggregate, local = self._explanations_for(snapshot_id, key)
            atom_ids: list[str] = []
            for rule in self._rules(aggregate, local):
                atom = self._atom(snapshot_id, group_key, key, endpoint, rule)
                if atom is None:
                    continue
                identifier = atom["atom_id"]
                atom_ids.append(identifier)
                existing = atoms.get(identifier)
                if existing is None:
                    atoms[identifier] = atom
                else:
                    self._merge(existing, atom)
            integrations.extend(
                self._integrations(key, endpoint, aggregate, local)
            )
            interfaces.append(
                {
                    "interface_key": key,
                    "method": method,
                    "path": path,
                    "handler": handler,
                    "group_key": group_key,
                    "file": endpoint.get("file") or "",
                    "line": endpoint.get("line") or 0,
                    "rule_count": len(dict.fromkeys(atom_ids)),
                    "atom_ids": list(dict.fromkeys(atom_ids)),
                }
            )

        self._attach_rules(interfaces, atoms)
        groups = self._groups(interfaces, atoms)
        service_rules = self._service_rules(atoms)
        integration_summary = self._integration_summary(integrations)
        covered = sum(1 for item in interfaces if item["rule_count"])
        summary = {
            "interface_count": len(interfaces),
            "interfaces_with_rules": covered,
            "rule_count": len(atoms),
            "group_count": len(groups),
            "shared_rule_count": sum(
                1 for atom in atoms.values() if len(atom["interface_keys"]) >= 2
            ),
            "coverage": round(covered / len(interfaces), 4) if interfaces else 0.0,
            "integration_count": integration_summary["total"],
            "cross_service_integration_count": integration_summary["cross_service_count"],
        }
        return {
            "version": RULE_MAP_VERSION,
            "repository_snapshot_id": snapshot_id,
            "summary": summary,
            "service": {
                "rule_count": len(service_rules),
                "shared_rule_count": sum(1 for item in service_rules if item["role"] == "shared"),
                "rules": service_rules,
            },
            "groups": groups,
            "interfaces": interfaces,
            "integrations": integration_summary,
            "generated_from": "api_explanations",
        }

    # ---- explanation resolution -----------------------------------------

    def _explanations_for(self, snapshot_id: str, key: str) -> tuple[dict, dict]:
        if self.store is None:
            return {}, {}
        try:
            snapshots = self.store.list_snapshots_for_repository_snapshot(snapshot_id, key)
        except (AttributeError, OSError, ValueError):
            return {}, {}
        for snapshot in snapshots:
            node = self.store.get_node(snapshot.id, snapshot.entry_node_key)
            if node is None:
                continue
            aggregate = _as_dict(node.aggregate_explanation)
            local = _as_dict(node.local_explanation)
            if aggregate or local:
                return aggregate, local
        return {}, {}

    @staticmethod
    def _rules(aggregate: dict, local: dict) -> list[Any]:
        for explanation in (aggregate, local):
            rules = explanation.get("business_rules")
            if isinstance(rules, list) and rules:
                return rules
        return []

    # ---- atoms -----------------------------------------------------------

    def _atom(
        self, snapshot_id: str, group_key: str, key: str, endpoint: dict[str, Any], rule: Any
    ) -> dict[str, Any] | None:
        text = _rule_text(rule)
        normalized = normalize_rule_text(text)
        if not normalized:
            return None
        rule_type = _rule_field(rule, "rule_type", "type") or "business"
        identifier = hashlib.sha1(
            f"{snapshot_id}|{normalized}|{rule_type}".encode("utf-8")
        ).hexdigest()[:16]
        return {
            "atom_id": identifier,
            "text": text,
            "normalized_text": normalized,
            "rule_type": rule_type,
            "condition": _rule_field(rule, "condition"),
            "failure_mode": _rule_field(rule, "failure_mode", "failure"),
            "groups": [group_key],
            "interface_keys": [key],
            "evidence": [
                {
                    "method": str(endpoint.get("method") or "").upper(),
                    "path": endpoint.get("path") or "",
                    "handler": endpoint.get("handler") or "",
                    "file": endpoint.get("file") or "",
                    "line": endpoint.get("line") or 0,
                    "source_node": _rule_field(rule, "source_node", "node_key"),
                    "lines": _rule_lines(rule),
                }
            ],
        }

    @staticmethod
    def _merge(target: dict[str, Any], incoming: dict[str, Any]) -> None:
        for kind, key in (("groups", "groups"), ("interface_keys", "interface_keys")):
            for value in incoming[key]:
                if value not in target[kind]:
                    target[kind].append(value)
        target["evidence"].extend(incoming["evidence"])

    @staticmethod
    def _attach_rules(
        interfaces: list[dict[str, Any]], atoms: dict[str, dict[str, Any]]
    ) -> None:
        for interface in interfaces:
            interface["rules"] = [atoms[atom_id] for atom_id in interface["atom_ids"]]

    # ---- integration contracts ------------------------------------------

    def _integrations(
        self, key: str, endpoint: dict[str, Any], aggregate: dict, local: dict
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for explanation in (aggregate, local):
            contracts = explanation.get("integration_contracts")
            if not isinstance(contracts, list):
                continue
            for contract in contracts:
                if not isinstance(contract, dict):
                    continue
                target = str(contract.get("target") or "").strip()
                if not target:
                    continue
                kind = _integration_kind(contract.get("kind"))
                identifier = hashlib.sha1(
                    f"{key}|{kind}|{target}|{contract.get('operation') or ''}".encode("utf-8")
                ).hexdigest()[:16]
                items.append(
                    {
                        "integration_id": identifier,
                        "kind": kind,
                        "scope": "cross_service" if kind in CROSS_SERVICE_KINDS else "resource",
                        "target": target,
                        "operation": str(contract.get("operation") or "").strip(),
                        "input_or_key": str(contract.get("input_or_key") or "").strip(),
                        "result_usage": str(contract.get("result_usage") or "").strip(),
                        "source_interface": key,
                        "method": str(endpoint.get("method") or "").upper(),
                        "path": endpoint.get("path") or "",
                        "handler": endpoint.get("handler") or "",
                        "file": endpoint.get("file") or "",
                        "line": endpoint.get("line") or 0,
                        "evidence_lines": _rule_lines(contract),
                    }
                )
            if items:
                break
        deduped: dict[str, dict[str, Any]] = {}
        for item in items:
            deduped.setdefault(item["integration_id"], item)
        return list(deduped.values())

    @staticmethod
    def _integration_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        by_kind: dict[str, int] = defaultdict(int)
        by_target: dict[str, int] = defaultdict(int)
        for item in items:
            by_kind[item["kind"]] += 1
            by_target[item["target"]] += 1
        return {
            "total": len(items),
            "cross_service_count": sum(
                1 for item in items if item["scope"] == "cross_service"
            ),
            "by_kind": [
                {"kind": kind, "count": count} for kind, count in sorted(by_kind.items())
            ],
            "by_target": [
                {"target": target, "count": count}
                for target, count in sorted(by_target.items(), key=lambda item: (-item[1], item[0]))
            ],
            "items": sorted(
                items,
                key=lambda item: (item["scope"] != "cross_service", item["kind"], item["target"]),
            ),
        }

    # ---- group / service projections -------------------------------------

    def _groups(
        self, interfaces: list[dict[str, Any]], atoms: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for interface in interfaces:
            by_group[interface["group_key"]].append(interface)
        groups = []
        for group_key in sorted(by_group):
            members = by_group[group_key]
            atom_ids = list(
                dict.fromkeys(atom_id for member in members for atom_id in member["atom_ids"])
            )
            group_atoms = [atoms[atom_id] for atom_id in atom_ids]
            groups.append(
                {
                    "group_key": group_key,
                    "surface": "api",
                    "kind": "resource",
                    "interface_count": len(members),
                    "interfaces_with_rules": sum(1 for item in members if item["rule_count"]),
                    "rule_count": len(group_atoms),
                    "shared_rule_count": sum(
                        1 for atom in group_atoms if len(atom["interface_keys"]) >= 2
                    ),
                    "coverage": round(
                        sum(1 for item in members if item["rule_count"]) / len(members), 4
                    ),
                    "interfaces": [
                        {
                            "interface_key": item["interface_key"],
                            "method": item["method"],
                            "path": item["path"],
                            "rule_count": item["rule_count"],
                        }
                        for item in sorted(members, key=lambda item: item["interface_key"])
                    ],
                    "rules": [self._rule_payload(atom, "interface_keys") for atom in group_atoms],
                }
            )
        return groups

    def _service_rules(self, atoms: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        rules = []
        for atom in atoms.values():
            payload = self._rule_payload(atom, "groups")
            payload["role"] = (
                "shared"
                if len(atom["groups"]) >= 2 or len(atom["interface_keys"]) >= 2
                else "local"
            )
            payload["source_interfaces"] = atom["interface_keys"]
            rules.append(payload)
        return sorted(rules, key=lambda item: (item["role"] != "shared", item["text"]))

    @staticmethod
    def _rule_payload(atom: dict[str, Any], shared_key: str = "interface_keys") -> dict[str, Any]:
        return {
            "atom_id": atom["atom_id"],
            "text": atom["text"],
            "rule_type": atom["rule_type"],
            "condition": atom["condition"],
            "failure_mode": atom["failure_mode"],
            "role": "shared" if len(atom[shared_key]) >= 2 else "local",
            "groups": atom["groups"],
            "source_interfaces": atom["interface_keys"],
            "evidence": atom["evidence"],
        }
