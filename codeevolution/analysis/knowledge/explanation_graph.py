"""Pure call-graph planning for bottom-up API explanations.

Edges point from caller to callee.  The planner condenses recursive strongly
connected components and returns a deterministic order in which every callee
component precedes its callers.  It deliberately has no CodeGraph dependency so
generation services can also use it with a frozen snapshot of the graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


def _node_key(node: str | Mapping[str, Any]) -> str:
    if isinstance(node, str):
        return node
    for field in ("node_key", "id", "key"):
        value = node.get(field)
        if value is not None and str(value):
            return str(value)
    raise ValueError("node requires node_key, id, or key")


@dataclass(frozen=True, order=True)
class CallEdge:
    caller: str
    callee: str


@dataclass(frozen=True)
class StronglyConnectedComponent:
    component_id: str
    node_keys: tuple[str, ...]
    cyclic: bool


@dataclass(frozen=True)
class ExplanationGraphPlan:
    """A frozen, deterministic schedule for node explanation."""

    node_keys: tuple[str, ...]
    edges: tuple[CallEdge, ...]
    components: tuple[StronglyConnectedComponent, ...]
    component_order: tuple[str, ...]
    node_order: tuple[str, ...]
    shared_nodes: tuple[str, ...]
    truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()

    @property
    def component_by_node(self) -> dict[str, str]:
        return {
            node: component.component_id
            for component in self.components
            for node in component.node_keys
        }


def _edge(edge: CallEdge | Mapping[str, Any] | tuple[str, str]) -> CallEdge:
    if isinstance(edge, CallEdge):
        return edge
    if isinstance(edge, tuple) and len(edge) == 2:
        return CallEdge(str(edge[0]), str(edge[1]))
    if isinstance(edge, Mapping):
        caller = edge.get("caller", edge.get("source"))
        callee = edge.get("callee", edge.get("target"))
        if caller is not None and callee is not None:
            return CallEdge(str(caller), str(callee))
    raise ValueError("edge requires caller/callee or source/target")


def build_explanation_plan(
    nodes: Iterable[str | Mapping[str, Any]],
    edges: Iterable[CallEdge | Mapping[str, Any] | tuple[str, str]],
    *,
    truncated: bool = False,
    truncation_reasons: Iterable[str] = (),
) -> ExplanationGraphPlan:
    """Condense a call graph and return its leaf-to-root processing plan.

    Nodes mentioned only by an edge are retained.  This makes an incomplete
    frozen graph visible to coverage validation rather than silently dropping
    it.  Duplicate edges and shared callees are represented once.
    """

    keys = {_node_key(node) for node in nodes}
    normal_edges = {_edge(edge) for edge in edges}
    for edge in normal_edges:
        keys.update((edge.caller, edge.callee))
    adjacency = {key: set() for key in keys}
    incoming_count = {key: 0 for key in keys}
    self_loops: set[str] = set()
    for edge in normal_edges:
        adjacency[edge.caller].add(edge.callee)
        if edge.caller == edge.callee:
            self_loops.add(edge.caller)
        else:
            incoming_count[edge.callee] += 1

    # Tarjan SCC; sorted traversal makes identifiers and output reproducible.
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    raw_components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for child in sorted(adjacency[node]):
            if child not in indices:
                visit(child)
                lowlinks[node] = min(lowlinks[node], lowlinks[child])
            elif child in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[child])
        if lowlinks[node] == indices[node]:
            members: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                members.append(member)
                if member == node:
                    break
            raw_components.append(tuple(sorted(members)))

    for key in sorted(keys):
        if key not in indices:
            visit(key)

    raw_components.sort(key=lambda members: members)
    components = tuple(
        StronglyConnectedComponent(
            component_id=f"scc:{idx}",
            node_keys=members,
            cyclic=len(members) > 1 or members[0] in self_loops,
        )
        for idx, members in enumerate(raw_components)
    )
    component_by_node = {
        node: component.component_id for component in components for node in component.node_keys
    }
    component_members = {component.component_id: component.node_keys for component in components}
    dag_children = {component.component_id: set() for component in components}
    dag_parents = {component.component_id: set() for component in components}
    for edge in normal_edges:
        caller = component_by_node[edge.caller]
        callee = component_by_node[edge.callee]
        if caller != callee:
            dag_children[caller].add(callee)
            dag_parents[callee].add(caller)

    # Kahn from sinks (callees) toward roots (callers).
    ready = sorted(
        (cid for cid, children in dag_children.items() if not children),
        key=lambda cid: component_members[cid],
    )
    ordered_components: list[str] = []
    remaining_children = {cid: set(children) for cid, children in dag_children.items()}
    while ready:
        current = ready.pop(0)
        ordered_components.append(current)
        for parent in sorted(dag_parents[current], key=lambda cid: component_members[cid]):
            remaining_children[parent].discard(current)
            if not remaining_children[parent] and parent not in ordered_components and parent not in ready:
                ready.append(parent)
        ready.sort(key=lambda cid: component_members[cid])

    node_order = tuple(
        node for cid in ordered_components for node in component_members[cid]
    )
    shared = tuple(sorted(node for node, count in incoming_count.items() if count > 1))
    reasons = tuple(dict.fromkeys(str(reason) for reason in truncation_reasons if str(reason)))
    return ExplanationGraphPlan(
        node_keys=tuple(sorted(keys)),
        edges=tuple(sorted(normal_edges)),
        components=components,
        component_order=tuple(ordered_components),
        node_order=node_order,
        shared_nodes=shared,
        truncated=bool(truncated or reasons),
        truncation_reasons=reasons,
    )


class ExplanationGraphAnalyzer:
    """Small callable facade for dependency injection in application services."""

    def build(self, nodes: Iterable, edges: Iterable, **kwargs: Any) -> ExplanationGraphPlan:
        return build_explanation_plan(nodes, edges, **kwargs)
