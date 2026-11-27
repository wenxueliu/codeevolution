"""Class-aware Mermaid sequence diagrams for API call chains.

The renderer is a pure function of frozen CodeGraph facts: the call-chain
nodes, the call edges between them and the class-containment edges that map a
method to its owning class.  Keeping it pure lets the Web read boundary
regenerate a diagram for snapshots published by an older extractor without
touching the immutable snapshot facts.

Lifelines represent types (classes/interfaces) and, for module-level
functions, source files.  Messages carry the called method name, so a diagram
shows both the class topology and the method order.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SEQUENCE_VERSION = 2
SEQUENCE_MARKER = f"%% sequence-v{SEQUENCE_VERSION}"
_MARKER_PATTERN = re.compile(r"(?m)^\s*%%\s*sequence-v(\d+)\s*$")

# CodeGraph node kinds that can own methods and therefore become lifelines.
CONTAINER_KINDS = frozenset(
    {"class", "interface", "record", "struct", "trait", "type", "enum", "object"}
)


@dataclass(frozen=True)
class Lifeline:
    """One sequence-diagram participant (a type or a source file)."""

    key: str
    label: str
    qualified_name: str = ""
    kind: str = "class"
    file_path: str = ""


def is_current_sequence(text: str | None) -> bool:
    """Whether a rendered diagram was produced by the current renderer."""
    match = _MARKER_PATTERN.search(text or "")
    return bool(match) and int(match.group(1)) >= SEQUENCE_VERSION


def render_sequence_diagram(
    entry_id: str,
    nodes: Sequence[Mapping[str, Any]],
    edges: Iterable[Mapping[str, Any]] = (),
    owners: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Render a Mermaid sequence diagram for one API entry point.

    ``nodes`` is the call-chain node list (``id``/``name``/``qualified_name``/
    ``kind``/``file_path``); ``edges`` are ``{source, target}`` call edges and
    ``owners`` maps a callable node id to its owning type node.
    """
    callable_nodes = {
        str(node["id"]): node
        for node in nodes or []
        if isinstance(node, Mapping) and node.get("id")
    }
    lines = ["sequenceDiagram", f"    {SEQUENCE_MARKER}"]
    if not callable_nodes:
        lines.append("    participant N0 as Unknown")
        lines.append("    Note over N0: No downstream calls")
        return "\n".join(lines)

    entry = str(entry_id or "")
    if entry not in callable_nodes:
        entry = next(iter(callable_nodes))

    position = {node_id: index for index, node_id in enumerate(callable_nodes)}
    adjacency: dict[str, list[tuple[int, str]]] = {}
    for edge in edges or []:
        if not isinstance(edge, Mapping):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source not in callable_nodes or target not in callable_nodes:
            continue
        call_line = edge.get("line")
        key = call_line if isinstance(call_line, int) and call_line > 0 else 1 << 30
        adjacency.setdefault(source, []).append((key, target))
    # Calls are emitted in source order: call-site line first, then chain
    # position as a stable tie-breaker for synthesized/heuristic edges.
    ordered_adjacency: dict[str, list[str]] = {}
    for source, targets in adjacency.items():
        targets.sort(key=lambda item: (item[0], position.get(item[1], 1 << 30)))
        ordered_adjacency[source] = [target for _, target in targets]

    order: list[tuple[str, str]] = []
    visited: set[tuple[str, str]] = set()
    _walk(entry, ordered_adjacency, visited, order)

    lifelines: dict[str, Lifeline] = {}
    node_keys: dict[str, str] = {}

    def lifeline_key(node_id: str) -> str:
        if node_id not in node_keys:
            lifeline = _resolve_lifeline(callable_nodes[node_id], owners)
            lifelines.setdefault(lifeline.key, lifeline)
            node_keys[node_id] = lifeline.key
        return node_keys[node_id]

    entry_key = lifeline_key(entry)
    for source, target in order:
        lifeline_key(source)
        lifeline_key(target)

    alias = {key: f"N{index}" for index, key in enumerate(lifelines)}
    labels = _unique_labels(lifelines)
    for key in lifelines:
        lines.append(f"    participant {alias[key]} as {_safe_label(labels[key])}")

    if not order:
        lines.append(f"    Note over {alias[entry_key]}: No downstream calls")
        return "\n".join(lines)

    for source, target in order:
        method = _safe_message(str(callable_nodes[target].get("name") or "call"))
        source_alias = alias[node_keys[source]]
        target_alias = alias[node_keys[target]]
        if source_alias == target_alias:
            lines.append(f"    {source_alias}->>{target_alias}: {method}()")
            continue
        lines.append(f"    {source_alias}->>+{target_alias}: {method}()")
        lines.append(f"    {target_alias}-->>-{source_alias}: returns")
    return "\n".join(lines)


def render_api_sequence_diagrams(
    source: Any,
    chains: Mapping[Any, tuple[str, Sequence[Mapping[str, Any]]]],
) -> dict[Any, str]:
    """Render several diagrams with batched ownership and edge queries.

    ``chains`` maps a caller key to ``(entry_node_id, chain_nodes)``.  A source
    without class ownership still renders source-file lifelines, so this stays
    usable with lighter graph adapters.
    """
    pending: list[tuple[Any, str, list[Mapping[str, Any]]]] = []
    for key, value in chains.items():
        entry, nodes = value
        usable = [
            node
            for node in nodes or []
            if isinstance(node, Mapping) and node.get("id")
        ]
        if usable:
            pending.append((key, str(entry or usable[0]["id"]), usable))
    if not pending:
        return {}

    node_ids = list(
        dict.fromkeys(str(node["id"]) for _, _, nodes in pending for node in nodes)
    )
    owners: Mapping[str, Mapping[str, Any]] = {}
    edges: list[Mapping[str, Any]] = []
    try:
        if hasattr(source, "call_chain_owners"):
            owners = source.call_chain_owners(node_ids) or {}
    except Exception:
        owners = {}
    try:
        if hasattr(source, "call_edges_between"):
            edges = list(source.call_edges_between(node_ids) or [])
    except Exception:
        edges = []

    rendered: dict[Any, str] = {}
    for key, entry, nodes in pending:
        node_set = {str(node["id"]) for node in nodes}
        local_edges = [
            edge
            for edge in edges
            if str(edge.get("source") or "") in node_set
            and str(edge.get("target") or "") in node_set
        ]
        rendered[key] = render_sequence_diagram(entry, nodes, local_edges, owners)
    return rendered


def _walk(
    node_id: str,
    adjacency: Mapping[str, Sequence[str]],
    visited: set[tuple[str, str]],
    order: list[tuple[str, str]],
) -> None:
    """Depth-first call walk, recorded in diagram order."""
    stack = [(node_id, iter(adjacency.get(node_id, ())))]
    while stack:
        current, targets = stack[-1]
        target = next(targets, None)
        if target is None:
            stack.pop()
            continue
        edge = (current, target)
        if edge in visited:
            continue
        visited.add(edge)
        order.append(edge)
        stack.append((target, iter(adjacency.get(target, ()))))


def _resolve_lifeline(
    node: Mapping[str, Any],
    owners: Mapping[str, Mapping[str, Any]] | None,
) -> Lifeline:
    owner = (owners or {}).get(str(node.get("id") or ""))
    if owner and owner.get("name"):
        qualified_name = str(owner.get("qualified_name") or owner["name"])
        return Lifeline(
            key="type:" + str(owner.get("id") or qualified_name),
            label=str(owner["name"]),
            qualified_name=qualified_name,
            kind=str(owner.get("kind") or "class"),
            file_path=str(owner.get("file_path") or node.get("file_path") or ""),
        )
    file_path = str(node.get("file_path") or "")
    label = _module_label(file_path) or str(node.get("name") or "module")
    return Lifeline(
        key="module:" + (file_path or label),
        label=label,
        qualified_name=file_path,
        kind="module",
        file_path=file_path,
    )


def _module_label(file_path: str) -> str:
    parts = [part for part in re.split(r"[\\/]", file_path or "") if part]
    if not parts:
        return ""
    name = re.sub(r"\.[^.]+$", "", parts[-1]) or parts[-1]
    return f"{parts[-2]}.{name}" if len(parts) >= 3 else name


def _unique_labels(lifelines: Mapping[str, Lifeline]) -> dict[str, str]:
    base = {key: lifeline.label for key, lifeline in lifelines.items()}
    counts = Counter(base.values())
    labels: dict[str, str] = {}
    used: set[str] = set()
    for key, lifeline in lifelines.items():
        label = base[key]
        if counts[label] > 1:
            label = _qualified_label(lifeline) or label
        candidate = label
        suffix = 2
        while candidate in used:
            candidate = f"{label} ({suffix})"
            suffix += 1
        used.add(candidate)
        labels[key] = candidate
    return labels


def _qualified_label(lifeline: Lifeline) -> str:
    if lifeline.kind == "module":
        path = re.sub(r"\.[^./\\]+$", "", lifeline.file_path or lifeline.qualified_name)
        parts = [part for part in re.split(r"[\\/]", path) if part]
        return ".".join(parts[-3:])
    text = (lifeline.qualified_name or lifeline.label).replace("::", ".")
    parts = [part for part in text.split(".") if part]
    return ".".join(parts[-3:])


def _safe_label(value: str) -> str:
    text = re.sub(r'["\r\n;]+', " ", str(value))
    return re.sub(r"\s+", " ", text).strip() or "unknown"


def _safe_message(value: str) -> str:
    return re.sub(r"[\r\n;]+", " ", value).strip() or "call"
