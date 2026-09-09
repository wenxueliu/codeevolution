"""MCP tools for immutable repository-analysis snapshots.

This boundary has no repository-path parameters. All graph and source access
is performed through ``SnapshotQueryService`` against frozen evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from fastmcp import FastMCP

from .application.snapshot_query_service import SnapshotQueryService
from .application.snapshot_runtime import SnapshotRuntime
from .application.snapshot_topology_service import SnapshotTopologyService

mcp = FastMCP("codeevolution")

# Kept as non-tool compatibility shims for embedders importing the old Python
# names. They are intentionally not registered with FastMCP and cannot perform
# Evolution/live-repository reads.
def get_feature_timeline(feature_name: str): return _result({"error": "No store configured"})
def list_features(): return _result({"error": "No store configured"})
def get_stats(): return _result({"error": "No store configured"})
def search_feature_history(query: str): return _result({"error": "No store configured"})
def get_feature_summary(feature_name: str): return _result({"error": "No store configured"})


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _result(value: Any) -> str:
    return json.dumps(_jsonable(value), indent=2, ensure_ascii=False)


def get_runtime() -> SnapshotRuntime | None:
    return getattr(mcp, "_snapshot_runtime", None)


def get_queries() -> SnapshotQueryService | None:
    return getattr(mcp, "_snapshot_queries", None)


def set_context(runtime: SnapshotRuntime) -> None:
    """Set the snapshot application facade for this MCP server instance."""
    mcp._snapshot_runtime = runtime
    mcp._snapshot_queries = runtime.snapshot_queries


def _require_runtime() -> SnapshotRuntime | str:
    runtime = get_runtime()
    return runtime if runtime is not None else _result({"error": "No snapshot runtime configured"})


def _require_queries() -> SnapshotQueryService | str:
    queries = get_queries()
    return queries if queries is not None else _result({"error": "No snapshot runtime configured"})


@mcp.tool()
def list_snapshot_catalog() -> str:
    """List scopes, members, and their current snapshots.

    Registered paths are intentionally omitted, so a client cannot induce a
    live workspace read.
    """
    runtime = _require_runtime()
    if isinstance(runtime, str):
        return runtime
    scopes = []
    for scope in runtime.store.list_scopes():
        members = []
        for member in runtime.store.list_members(scope.id):
            current = runtime.store.get_current_snapshot(member.id)
            members.append({
                "id": member.id,
                "display_name": member.display_name,
                "current_snapshot_id": current.id if current else None,
            })
        scopes.append({"id": scope.id, "name": scope.name, "members": members})
    return _result({"scopes": scopes})


@mcp.tool()
def get_snapshot(snapshot_id: str) -> str:
    """Get immutable metadata for a repository snapshot by ID."""
    runtime = _require_runtime()
    if isinstance(runtime, str):
        return runtime
    snapshot = runtime.store.get_snapshot(snapshot_id)
    if snapshot is None:
        return _result({"error": "Snapshot not found", "snapshot_id": snapshot_id})
    metadata = asdict(snapshot)
    metadata.pop("facts", None)
    metadata.pop("facts_artifact_key", None)
    return _result(metadata)


@mcp.tool()
def get_snapshot_facts(snapshot_id: str, section: str = "") -> str:
    """Read immutable knowledge facts from a snapshot, optionally one section."""
    queries = _require_queries()
    if isinstance(queries, str):
        return queries
    try:
        return _result(queries.knowledge(snapshot_id, section=section or None))
    except KeyError:
        return _result({"error": "Snapshot or knowledge section not found", "snapshot_id": snapshot_id})
    except RuntimeError as error:
        return _result({"error": str(error), "snapshot_id": snapshot_id})


@mcp.tool()
def search_snapshot_symbols(snapshot_id: str, query: str) -> str:
    """Search frozen graph symbols in a repository snapshot."""
    queries = _require_queries()
    if isinstance(queries, str):
        return queries
    try:
        with queries.open(snapshot_id) as handle:
            return _result({"snapshot_id": snapshot_id, "query": query,
                            "symbols": handle.graph.functions_named_like(query)})
    except KeyError:
        return _result({"error": "Snapshot not found", "snapshot_id": snapshot_id})
    except RuntimeError as error:
        return _result({"error": str(error), "snapshot_id": snapshot_id})


@mcp.tool()
def get_snapshot_call_tree(snapshot_id: str, node_id: str) -> str:
    """Get one frozen call-tree expansion from a repository snapshot."""
    queries = _require_queries()
    if isinstance(queries, str):
        return queries
    try:
        return _result(queries.call_tree_children(snapshot_id, node_id))
    except KeyError:
        return _result({"error": "Snapshot not found", "snapshot_id": snapshot_id})
    except RuntimeError as error:
        return _result({"error": str(error), "snapshot_id": snapshot_id})


@mcp.tool()
def get_snapshot_rule_context(snapshot_id: str, node_id: str) -> str:
    """Get frozen source context for a node, suitable for rule explanations."""
    queries = _require_queries()
    if isinstance(queries, str):
        return queries
    try:
        context = queries.node_rule_context(snapshot_id, node_id)
        if context is None:
            return _result({"error": "Node not found", "snapshot_id": snapshot_id, "node_id": node_id})
        return _result(context)
    except KeyError:
        return _result({"error": "Snapshot not found", "snapshot_id": snapshot_id})
    except RuntimeError as error:
        return _result({"error": str(error), "snapshot_id": snapshot_id})


def _topology_service() -> SnapshotTopologyService | str:
    runtime = _require_runtime()
    if isinstance(runtime, str):
        return runtime
    return SnapshotTopologyService(runtime.store, runtime.snapshot_queries)


@mcp.tool()
def get_view_topology(view_id: str) -> str:
    """Project topology from the immutable snapshots in a Graph View."""
    service = _topology_service()
    if isinstance(service, str):
        return service
    try:
        return _result(service.topology(view_id))
    except KeyError:
        return _result({"error": "Graph View not found", "view_id": view_id})


@mcp.tool()
def get_view_impact(view_id: str, service: str) -> str:
    """Compute impact using only a pinned snapshot membership."""
    topology = _topology_service()
    if isinstance(topology, str):
        return topology
    try:
        return _result(topology.impact(view_id, service))
    except KeyError:
        return _result({"error": "Graph View not found", "view_id": view_id})


@mcp.tool()
def get_view_flow(view_id: str, service: str, path: str = "") -> str:
    """Trace a flow projection within a frozen Graph View."""
    topology = _topology_service()
    if isinstance(topology, str):
        return topology
    try:
        return _result(topology.flow(view_id, service, path))
    except KeyError:
        return _result({"error": "Graph View not found", "view_id": view_id})


@mcp.tool()
def get_view_entities(view_id: str) -> str:
    """Align entity facts across members of an immutable Graph View."""
    topology = _topology_service()
    if isinstance(topology, str):
        return topology
    try:
        return _result(topology.entities(view_id))
    except KeyError:
        return _result({"error": "Graph View not found", "view_id": view_id})


def run_server(runtime: SnapshotRuntime, transport: str = "stdio") -> None:
    """Start MCP with the snapshot-only application facade."""
    set_context(runtime)
    mcp.run(transport=transport)
