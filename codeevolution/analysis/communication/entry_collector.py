"""Production entry-point and frozen call-reachability helpers."""

from __future__ import annotations

import re
from collections import deque
from hashlib import sha256
from typing import Any

from codeevolution.analysis.communication.schema import EntryFact, Location, NodeRef


def collect_entries(
    graph,
    snapshot_id: str,
    *,
    max_entries: int = 5000,
    allowed_paths: set[str] | frozenset[str] | None = None,
    return_coverage: bool = False,
) -> tuple[EntryFact, ...] | tuple[tuple[EntryFact, ...], dict[str, Any]]:
    """Convert CodeGraph entry declarations into stable Snapshot facts."""
    result: list[EntryFact] = []
    candidates = sorted(
        graph.inbound_endpoints(), key=lambda item: (item.file_path, item.start_line, item.qualified_name, item.node_id)
    )
    eligible = [entry for entry in candidates if not _excluded_path(entry.file_path)]
    selected = eligible[: max(0, max_entries)]
    for entry in selected:
        if _excluded_path(entry.file_path):
            continue
        method, path = _http_identity(entry)
        kind = _entry_kind(entry.entry_type, method, path)
        if kind == "grpc_server" and not method:
            method = _grpc_method(entry)
        protocol = "http" if kind == "http" else kind
        identity = {
            "kind": kind,
            "protocol": protocol,
            "method": method,
            "path_template": path,
            "handler": {
                "qualified_name": entry.qualified_name,
                "file": entry.file_path,
                "start_line": entry.start_line,
            },
        }
        entry_id = "entry:sha256:" + sha256(_canonical(identity).encode()).hexdigest()
        handler = NodeRef(
            snapshot_id=snapshot_id,
            node_id=entry.node_id,
            kind="method" if "method" in entry.qualified_name.lower() else "function",
            name=entry.name,
            qualified_name=entry.qualified_name,
            location=(
                Location(entry.file_path, entry.start_line)
                if allowed_paths is None or entry.file_path in allowed_paths
                else None
            ),
        )
        result.append(
            EntryFact(
                entry_id=entry_id,
                kind=kind,
                protocol=protocol,
                handler=handler,
                method=method,
                path_template=path,
                channel=None,
                evidence={"entry_type": entry.entry_type},
            )
        )
    output = tuple(result)
    if not return_coverage:
        return output
    return output, {
        "candidate_count": len(eligible),
        "emitted_count": len(output),
        "max_entries": max_entries,
        "truncated": len(eligible) > len(output),
        "excluded_count": len(candidates) - len(eligible),
    }


def reachable_call_paths(
    graph,
    entries: tuple[EntryFact, ...],
    *,
    max_depth: int = 12,
    max_nodes: int = 10000,
    max_edges: int = 50000,
    callee_cache: dict[str, tuple[Any, ...]] | None = None,
    return_coverage: bool = False,
) -> dict[str, dict[str, list[str]]] | tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, Any]]]:
    """Return shortest entry-rooted node paths using only frozen calls edges."""
    # The same frozen graph is traversed once per entry.  Cache the immutable
    # outgoing edge tuples so a large set of HTTP/MQ/RPC entries does not issue
    # repeated SQLite queries for shared call-tree nodes.
    cached_callees = callee_cache if callee_cache is not None else {}

    def _callees(node_id: str) -> tuple[Any, ...]:
        targets = cached_callees.get(node_id)
        if targets is None:
            targets = tuple(
                sorted(
                    graph.callees(node_id) or (),
                    key=lambda item: (item.callee_node_id, item.call_line, item.callee_name),
                )
            )
            cached_callees[node_id] = targets
        return targets

    result: dict[str, dict[str, list[str]]] = {}
    coverage: dict[str, dict[str, Any]] = {}
    for entry in entries:
        paths: dict[str, list[str]] = {entry.handler.node_id: [entry.handler.node_id]}
        depths: dict[str, int] = {entry.handler.node_id: 0}
        alternatives: dict[str, int] = {entry.handler.node_id: 0}
        edge_kinds: dict[str, list[str]] = {entry.handler.node_id: []}
        truncated = False
        edges_examined = 0
        queue = deque([(entry.handler.node_id, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                if _callees(current):
                    truncated = True
                continue
            targets = _callees(current)
            for target in targets:
                edges_examined += 1
                if edges_examined > max_edges:
                    truncated = True
                    break
                node_id = target.callee_node_id
                if node_id in paths:
                    if depths[node_id] == depth + 1:
                        alternatives[node_id] = alternatives.get(node_id, 0) + 1
                    continue
                if len(paths) >= max_nodes:
                    truncated = True
                    break
                paths[node_id] = paths[current] + [node_id]
                depths[node_id] = depth + 1
                alternatives[node_id] = 0
                edge_kinds[node_id] = edge_kinds[current] + [target.provenance or "calls"]
                queue.append((node_id, depth + 1))
        result[entry.entry_id] = paths
        coverage[entry.entry_id] = {
            "truncated": truncated,
            "max_depth": max_depth,
            "max_nodes": max_nodes,
            "max_edges": max_edges,
            "edges_examined": edges_examined,
            "node_count": len(paths),
            "depth": depths,
            "edge_kinds": edge_kinds,
            "alternative_shortest_path_count": alternatives,
            "reachability_rule": "shortest-call-path/v1",
        }
    if not return_coverage:
        return result
    return result, coverage


def _entry_kind(entry_type: str, method: str | None, path: str | None) -> str:
    if method or path or str(entry_type).lower() in {"http", "route", "api", "endpoint"}:
        return "http"
    normalized = str(entry_type or "unknown").lower().replace(" ", "_")
    if "grpc" in normalized or "rpc" in normalized:
        return "grpc_server"
    if "consumer" in normalized or "message" in normalized or "event" in normalized:
        return "message_consumer"
    if "cron" in normalized or "sched" in normalized or "job" in normalized:
        return "scheduled_job"
    if "cli" in normalized or "command" in normalized:
        return "cli"
    return normalized


def _http_identity(entry) -> tuple[str | None, str | None]:
    method = entry.http_method.upper() if entry.http_method else None
    path = entry.http_path
    if method and path:
        return method, path
    for decorator in getattr(entry, "decorators", ()) or ():
        text = str(decorator)
        match = re.search(
            r"(?:^|[.@])(?P<method>get|post|put|patch|delete|head|options|route)\s*\(\s*['\"`]?(?P<path>/[^'\"`), ]*)",
            text,
            re.IGNORECASE,
        )
        if not match:
            continue
        candidate_method = match.group("method").upper()
        if candidate_method == "ROUTE":
            method_match = re.search(r"methods?\s*=\s*\[?['\"`]?(GET|POST|PUT|PATCH|DELETE|HEAD)", text, re.IGNORECASE)
            candidate_method = method_match.group(1).upper() if method_match else None
        return method or candidate_method, path or match.group("path")
    return method, path


def _grpc_method(entry) -> str | None:
    for decorator in getattr(entry, "decorators", ()) or ():
        match = re.search(r"(?:rpc|grpc|method)[^\(]*\(\s*['\"`]([^'\"`]+)", str(decorator), re.IGNORECASE)
        if match:
            return match.group(1)
    return entry.name if getattr(entry, "entry_type", "").lower() in {"grpc", "grpc_server", "rpc"} else None


def _excluded_path(path: str) -> bool:
    normalized = "/" + path.replace("\\", "/").lower() + "/"
    return any(token in normalized for token in ("/test/", "/tests/", "/benchmark/", "/benchmarks/", "/example/", "/examples/", "/migration/", "/migrations/", "/dev/"))


def _canonical(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["collect_entries", "reachable_call_paths"]
