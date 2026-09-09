"""Snapshot-only single-repository query use cases."""

from __future__ import annotations

from codeevolution.analysis.knowledge.node_rule import SnapshotNodeRuleService
from codeevolution.infrastructure.snapshot_bundle_resolver import (
    RepositorySnapshotHandle,
    SnapshotBundleResolver,
)


class SnapshotQueryService:
    """Project immutable facts and graph data from one published Snapshot.

    No method accepts a repository path or consults the registry; this is the
    boundary that makes deleting a registered checkout safe for historical
    reads.
    """

    def __init__(self, resolver: SnapshotBundleResolver):
        self.resolver = resolver

    def open(self, snapshot_id: str, *, member_id: str | None = None) -> RepositorySnapshotHandle:
        return self.resolver.open(snapshot_id, member_id=member_id)

    def view_snapshot(self, view_id: str, snapshot_id: str) -> str:
        view = self.resolver.store.get_view(view_id)
        if view is None:
            raise KeyError(view_id)
        if not any(item.snapshot_id == snapshot_id for item in view.members):
            raise ValueError("snapshot_view_mismatch")
        return snapshot_id

    def view_knowledge(self, view_id: str, *, member_id: str | None = None) -> dict:
        view = self.resolver.store.get_view(view_id)
        if view is None:
            raise KeyError(view_id)
        selected = [item for item in view.members if member_id is None or item.member_id == member_id]
        if member_id is not None and not selected:
            raise ValueError("member is not part of view")
        reports = []
        unavailable = []
        for item in selected:
            if item.snapshot_id is None:
                unavailable.append({"member_id": item.member_id, "availability": item.availability.value})
                continue
            report = self.knowledge(item.snapshot_id)
            report["member_id"] = item.member_id
            report["repository_snapshot_id"] = item.snapshot_id
            reports.append(report)
        if member_id is not None:
            return reports[0] if reports else {"member_id": member_id, "availability": unavailable[0]["availability"]}
        from .knowledge_service import merge_knowledge_reports
        merged = merge_knowledge_reports([(item["member_id"], item) for item in reports]) if reports else {"repositories": []}
        merged["view_id"] = view_id
        merged["view_digest"] = view.digest
        merged["unavailable_members"] = unavailable
        return merged

    def knowledge(self, snapshot_id: str, *, section: str | None = None) -> dict:
        with self.open(snapshot_id) as handle:
            facts = handle.snapshot.facts
            if facts is None:
                raise RuntimeError("snapshot facts are unavailable")
            # Older immutable snapshots predate ApiEndpoint.node_id.  Their
            # call_chain already records the entry CodeGraph id, so project it
            # at the read boundary without mutating the historical facts.
            facts = _project_api_node_ids(facts)
            if section is None:
                return facts
            aliases = {"gaps": "test_coverage", "tests": "test_coverage", "deps": "external_dependencies", "auth": "authorization_model", "layers": "layer_violations", "config": "config_consumption", "api": "api_contract", "modules": "module_topology", "entities": "core_entities", "heatmap": "heat_map"}
            key = aliases.get(section, section)
            if key not in facts:
                raise KeyError(section)
            return {key: facts[key]}

    def call_tree_children(self, snapshot_id: str, node_id: str) -> dict:
        with self.open(snapshot_id) as handle:
            return _children(handle, node_id)

    def node_rule_context(self, snapshot_id: str, node_id: str) -> dict | None:
        with self.open(snapshot_id) as handle:
            return SnapshotNodeRuleService().resolve(handle, node_id)


def _children(handle: RepositorySnapshotHandle, node_id: str) -> dict:
    node = handle.graph.get_function_by_id(node_id)
    if node is None:
        return {"repository_snapshot_id": handle.snapshot.id, "root": None, "children": [], "truncated": False}
    rows = handle.graph.get_callee_rows(node_id, 200)
    children = [{
        "type": "func", "id": row.get("id"), "node_id": row.get("id"),
        "repository_snapshot_id": handle.snapshot.id, "name": row.get("name"),
        "qualified_name": row.get("qualified_name"), "kind": row.get("kind"),
        "file": row.get("file_path"), "line": row.get("start_line"),
        "call_line": row.get("call_line"), "signature": row.get("signature"), "expandable": True,
    } for row in rows[:60]]
    return {
        "repository_snapshot_id": handle.snapshot.id,
        "root": {"id": node.node_id, "node_id": node.node_id, "name": node.name,
                 "qualified_name": node.qualified_name, "file": node.file_path,
                 "line": node.start_line},
        "children": children, "truncated": len(rows) > 60,
    }


def _project_api_node_ids(facts: dict) -> dict:
    api = facts.get("api_contract")
    if not isinstance(api, dict) or not isinstance(api.get("endpoints"), list):
        return facts
    endpoints = []
    changed = False
    for endpoint in api["endpoints"]:
        if not isinstance(endpoint, dict) or endpoint.get("node_id"):
            endpoints.append(endpoint)
            continue
        chain = endpoint.get("call_chain")
        entry_id = chain[0].get("id") if isinstance(chain, list) and chain and isinstance(chain[0], dict) else None
        if entry_id:
            endpoints.append({**endpoint, "node_id": entry_id})
            changed = True
        else:
            endpoints.append(endpoint)
    if not changed:
        return facts
    return {**facts, "api_contract": {**api, "endpoints": endpoints}}
