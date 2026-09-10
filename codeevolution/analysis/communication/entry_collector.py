"""Production entry-point and frozen call-reachability helpers."""

from __future__ import annotations

from collections import deque
from hashlib import sha256
from typing import Any

from codeevolution.analysis.communication.schema import EntryFact, Location, NodeRef


def collect_entries(graph, snapshot_id: str, *, max_entries: int = 5000) -> tuple[EntryFact, ...]:
    """Convert CodeGraph entry declarations into stable Snapshot facts."""
    result: list[EntryFact] = []
    for entry in sorted(graph.inbound_endpoints(), key=lambda item: (item.file_path, item.start_line, item.node_id))[:max_entries]:
        if _excluded_path(entry.file_path):
            continue
        kind = _entry_kind(entry.entry_type, entry.http_method, entry.http_path)
        protocol = "http" if kind == "http" else kind
        identity = {
            "kind": kind,
            "protocol": protocol,
            "method": entry.http_method,
            "path_template": entry.http_path,
            "handler": entry.node_id,
        }
        entry_id = "entry:sha256:" + sha256(_canonical(identity).encode()).hexdigest()
        handler = NodeRef(
            snapshot_id=snapshot_id,
            node_id=entry.node_id,
            kind="method" if "method" in entry.qualified_name.lower() else "function",
            name=entry.name,
            qualified_name=entry.qualified_name,
            location=Location(entry.file_path, entry.start_line),
        )
        result.append(
            EntryFact(
                entry_id=entry_id,
                kind=kind,
                protocol=protocol,
                handler=handler,
                method=entry.http_method,
                path_template=entry.http_path,
                channel=None,
                evidence={"entry_type": entry.entry_type},
            )
        )
    return tuple(result)


def reachable_call_paths(graph, entries: tuple[EntryFact, ...], *, max_depth: int = 12, max_nodes: int = 10000) -> dict[str, dict[str, list[str]]]:
    """Return shortest entry-rooted node paths using only frozen calls edges."""
    result: dict[str, dict[str, list[str]]] = {}
    for entry in entries:
        paths: dict[str, list[str]] = {entry.handler.node_id: [entry.handler.node_id]}
        queue = deque([(entry.handler.node_id, 0)])
        while queue and len(paths) <= max_nodes:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for target in graph.callees(current):
                node_id = target.callee_node_id
                if node_id in paths:
                    continue
                paths[node_id] = paths[current] + [node_id]
                queue.append((node_id, depth + 1))
                if len(paths) >= max_nodes:
                    break
        result[entry.entry_id] = paths
    return result


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


def _excluded_path(path: str) -> bool:
    normalized = "/" + path.replace("\\", "/").lower() + "/"
    return any(token in normalized for token in ("/test/", "/tests/", "/benchmark/", "/benchmarks/", "/example/", "/examples/", "/migration/", "/migrations/", "/dev/"))


def _canonical(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["collect_entries", "reachable_call_paths"]
