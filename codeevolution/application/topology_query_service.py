"""Synchronous Impact and StaticPossibleFlow queries over a Topology artifact."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any


class TopologyQueryError(ValueError):
    """Invalid query selector or limit."""


class TopologyQueryService:
    MAX_FLOW_NODES = 5_000
    MAX_FLOW_EDGES = 10_000
    MAX_DEPTH = 20

    def impact(
        self,
        artifact: Mapping[str, Any],
        member_id: str,
        *,
        direction: str = "both",
        max_depth: int = 5,
        channels: Iterable[str] = ("http", "message", "grpc"),
        include_resources: bool = True,
        include_shared_resource_risks: bool = False,
        include_candidates: bool = False,
        view_id: str | None = None,
    ) -> dict[str, Any]:
        self._check_depth(max_depth)
        if direction not in {"upstream", "downstream", "both"}:
            raise TopologyQueryError("invalid_direction")
        services = {item.get("member_id"): item for item in artifact.get("services", [])}
        if member_id not in services:
            raise TopologyQueryError("member_not_in_view")
        allowed = set(channels)
        edges = [
            item for item in artifact.get("service_projections", [])
            if item.get("kind") in allowed
        ]
        downstream = self._walk(member_id, edges, reverse=False, max_depth=max_depth) if direction in {"downstream", "both"} else {}
        upstream = self._walk(member_id, edges, reverse=True, max_depth=max_depth) if direction in {"upstream", "both"} else {}
        result: dict[str, Any] = {
            "service": member_id,
            "direction": direction,
            "downstream_dependencies": self._paths(downstream),
            "upstream_dependents": self._paths(upstream),
            "coverage": artifact.get("coverage", {}),
            "unknown_boundaries": artifact.get("coverage", {}).get("unknown_boundaries", []),
        }
        result["downstream_direct"] = [item for item in result["downstream_dependencies"] if item["depth"] == 1]
        result["downstream_transitive"] = [item for item in result["downstream_dependencies"] if item["depth"] > 1]
        result["upstream_direct"] = [item for item in result["upstream_dependents"] if item["depth"] == 1]
        result["upstream_transitive"] = [item for item in result["upstream_dependents"] if item["depth"] > 1]
        if include_resources:
            result["resources"] = [
                item for item in artifact.get("resource_dependencies", [])
                if item.get("source_member_id") == member_id
            ]
        if include_shared_resource_risks:
            result["potential_data_coupling"] = self._shared_resource_risks(artifact, member_id)
        if include_candidates:
            result["candidate_dependencies"] = [
                item for item in artifact.get("candidates", [])
                if item.get("source", {}).get("member_id") == member_id
                or item.get("source_member_id") == member_id
            ]
        result["message_alternatives"] = [
            item for item in artifact.get("message_alternatives", [])
            if item.get("source_member_id") == member_id
            or member_id in item.get("consumer_member_ids", [])
            or any(
                isinstance(consumer, Mapping) and consumer.get("member_id") == member_id
                for consumer in item.get("consumers", [])
            )
        ]
        result["unknown_frontier"] = [
            {"kind": "coverage", "member_id": item, "reason": "partial_coverage"}
            for item in result["unknown_boundaries"]
        ]
        if direction in {"upstream", "both"} and not upstream and result["unknown_boundaries"]:
            result["upstream_status"] = "no_confirmed_upstream_within_coverage"
        if direction in {"downstream", "both"} and not downstream and result["unknown_boundaries"]:
            result["downstream_status"] = "no_confirmed_downstream_within_coverage"
        if view_id is not None:
            result["view_id"] = view_id
        return result

    def flow(
        self,
        artifact: Mapping[str, Any],
        member_id: str,
        *,
        entry_id: str | None = None,
        method: str | None = None,
        path: str | None = None,
        max_depth: int = 8,
        max_nodes: int = 500,
        max_edges: int = 1000,
        channels: Iterable[str] = ("http", "message", "grpc"),
        include_resources: bool = False,
        include_candidates: bool = False,
        view_id: str | None = None,
    ) -> dict[str, Any]:
        self._check_depth(max_depth)
        if max_nodes < 1 or max_edges < 1:
            raise TopologyQueryError("invalid_flow_limits")
        if max_nodes > self.MAX_FLOW_NODES or max_edges > self.MAX_FLOW_EDGES:
            raise TopologyQueryError("flow_limits_exceeded")
        root_entry = self._resolve_entry(artifact, member_id, entry_id, method, path)
        root = {"member_id": member_id, "entry_id": root_entry}
        nodes: list[dict[str, Any]] = [{**root, "node_id": self._node_key(member_id, root_entry), "depth": 0}]
        edges: list[dict[str, Any]] = []
        references: list[dict[str, Any]] = []
        cycles: list[dict[str, Any]] = []
        visited = {self._node_key(member_id, root_entry)}
        queue = deque([(member_id, root_entry, 0)])
        allowed = set(channels)
        truncation = None
        dependencies = [
            item for item in artifact.get("endpoint_dependencies", [])
            if item.get("kind") in allowed
        ]
        # Competing consumers are not confirmed service edges, but Flow still
        # exposes their frozen alternatives as explicitly labelled branches.
        for group in artifact.get("message_alternatives", []):
            if "message" not in allowed:
                continue
            source_member = group.get("source_member_id")
            source_entry = group.get("source_entry_id") or group.get("channel")
            for consumer in group.get("consumers", []):
                if not isinstance(consumer, Mapping):
                    continue
                dependencies.append({
                    "kind": "message",
                    "edge_id": f"{group.get('alternative_group_id', 'message-alternative')}:{consumer.get('member_id')}:{consumer.get('entry_id')}",
                    "source": {"member_id": source_member, "entry_id": source_entry},
                    "target": {"member_id": consumer.get("member_id"), "entry_ids": [consumer.get("entry_id")]},
                    "confidence": {"level": "medium", "reasons": ["message_competing_alternative"]},
                    "alternative_group_id": group.get("alternative_group_id"),
                })
        while queue:
            source_member, source_entry, depth = queue.popleft()
            if depth >= max_depth:
                if any(self._source_matches(item, source_member, source_entry) for item in dependencies):
                    truncation = {"reason": "max_depth", "frontier": [{"member_id": source_member, "entry_id": source_entry}]}
                continue
            for dependency in dependencies:
                if not self._source_matches(dependency, source_member, source_entry):
                    continue
                targets = self._targets(dependency)
                for target in targets:
                    target_member = target.get("member_id")
                    target_entry = target.get("entry_id") or target.get("channel") or "unknown"
                    node_key = self._node_key(target_member, target_entry)
                    edge = {
                        "edge_id": dependency.get("edge_id", f"dependency:{len(edges)}"),
                        "kind": dependency.get("kind", "http"),
                        "source": {"member_id": source_member, "entry_id": source_entry},
                        "target": {"member_id": target_member, "entry_id": target_entry},
                        "depth": depth + 1,
                        "confidence": dependency.get("confidence", {}),
                    }
                    if dependency.get("alternative_group_id"):
                        edge["alternative_group_id"] = dependency["alternative_group_id"]
                    if len(edges) >= max_edges:
                        truncation = {"reason": "max_edges", "frontier": [{"member_id": source_member, "entry_id": source_entry}]}
                        break
                    edges.append(edge)
                    if node_key in visited:
                        cycles.append({"edge_id": edge["edge_id"], "target": edge["target"]})
                        references.append({"edge_id": edge["edge_id"], "reference": node_key})
                        continue
                    if len(nodes) >= max_nodes:
                        truncation = {"reason": "max_nodes", "frontier": [{"member_id": target_member, "entry_id": target_entry}]}
                        break
                    visited.add(node_key)
                    nodes.append({**edge["target"], "node_id": node_key, "depth": depth + 1})
                    queue.append((target_member, target_entry, depth + 1))
                if truncation and truncation["reason"] in {"max_edges", "max_nodes"}:
                    break
            if truncation and truncation["reason"] in {"max_edges", "max_nodes"}:
                break
        if include_resources:
            for resource in artifact.get("resource_dependencies", []):
                if resource.get("source_member_id") == member_id:
                    if len(nodes) >= max_nodes:
                        truncation = truncation or {
                            "reason": "max_nodes",
                            "frontier": [{"member_id": member_id, "entry_id": root_entry}],
                        }
                        break
                    nodes.append({"node_id": resource.get("edge_id"), "kind": "resource", "resource": resource})
        unknown_frontier = [
            {"kind": "coverage", "member_id": item, "reason": "partial_coverage"}
            for item in artifact.get("coverage", {}).get("unknown_boundaries", [])
        ]
        unknown_frontier.extend(
            {"kind": "candidate", "observation_id": item.get("observation_id"), "reason": item.get("reason", "candidate")}
            for item in artifact.get("candidates", [])
            if item.get("source", {}).get("member_id") == member_id or item.get("source_member_id") == member_id
        )
        result = {
            "root": root,
            "nodes": nodes,
            "edges": edges,
            "references": references,
            "cycles": cycles,
            "coverage": artifact.get("coverage", {}),
            "truncation": truncation,
            "unknown_frontier": unknown_frontier,
        }
        if include_candidates:
            result["candidate_edges"] = [
                item for item in artifact.get("candidates", [])
                if item.get("source", {}).get("member_id") == member_id
            ]
        if view_id is not None:
            result["view_id"] = view_id
        return result

    @staticmethod
    def _walk(start: str, edges: list[Mapping[str, Any]], *, reverse: bool, max_depth: int) -> dict[str, list[str]]:
        paths: dict[str, list[str]] = {}
        queue = deque([(start, [start], 0)])
        while queue:
            current, path, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for edge in edges:
                source, target = edge.get("source_member_id"), edge.get("target_member_id")
                next_member = source if reverse and target == current else target if not reverse and source == current else None
                if next_member is None or next_member in paths:
                    continue
                next_path = path + [next_member]
                paths[next_member] = next_path
                queue.append((next_member, next_path, depth + 1))
        return paths

    @staticmethod
    def _paths(paths: dict[str, list[str]]) -> list[dict[str, Any]]:
        return [{"member_id": member_id, "path": path, "depth": len(path) - 1} for member_id, path in sorted(paths.items())]

    @staticmethod
    def _shared_resource_risks(artifact: Mapping[str, Any], member_id: str) -> list[dict[str, Any]]:
        resources = artifact.get("resource_dependencies", [])
        own = {
            str(item.get("resource", {}).get("instance_id"))
            for item in resources
            if item.get("source_member_id") == member_id
            and str(item.get("resource", {}).get("instance_id")) not in {"", "None", "unresolved"}
        }
        result = []
        for resource in resources:
            if resource.get("source_member_id") != member_id and str(resource.get("resource", {}).get("instance_id")) in own:
                result.append({"kind": "potential_data_coupling", "resource": resource})
        return result

    @staticmethod
    def _resolve_entry(artifact, member_id, entry_id, method, path):
        if entry_id:
            for service in artifact.get("services", []):
                if service.get("member_id") != member_id:
                    continue
                if any(entry.get("entry_id") == entry_id for entry in service.get("entries", [])):
                    return entry_id
            raise TopologyQueryError("entry_not_in_view")
        if not method or not path:
            raise TopologyQueryError("entry_not_in_view")
        matches = []
        for service in artifact.get("services", []):
            if service.get("member_id") != member_id:
                continue
            for entry in service.get("entries", []):
                if str(entry.get("method", "")).upper() == method.upper() and entry.get("path_template") == path:
                    matches.append(entry.get("entry_id"))
        if len(matches) != 1:
            raise TopologyQueryError("ambiguous_entry" if matches else "entry_not_in_view")
        return matches[0]

    @staticmethod
    def _source_matches(dependency, member_id, entry_id):
        source = dependency.get("source", {})
        return source.get("member_id") == member_id and source.get("entry_id") == entry_id

    @staticmethod
    def _targets(dependency):
        target = dependency.get("target")
        if isinstance(target, Mapping):
            member_id = target.get("member_id")
            entries = target.get("entry_ids") or [target.get("entry_id")]
            return [{"member_id": member_id, "entry_id": entry} for entry in entries if member_id and entry]
        if dependency.get("target_member_id"):
            return [{"member_id": dependency["target_member_id"], "entry_id": dependency.get("channel")}]
        return []

    @staticmethod
    def _node_key(member_id, entry_id):
        return f"{member_id}:{entry_id}"

    @staticmethod
    def _check_depth(max_depth):
        if max_depth < 0 or max_depth > TopologyQueryService.MAX_DEPTH:
            raise TopologyQueryError("invalid_max_depth")


__all__ = ["TopologyQueryError", "TopologyQueryService"]
