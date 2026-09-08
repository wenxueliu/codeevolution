"""Deterministic completeness checks for an API explanation candidate."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

_SUCCESS = {"completed", "reused"}
_FACT_FIELDS = (
    "business_rules",
    "state_changes",
    "side_effects",
    "exceptions",
    "external_dependencies",
    "database_writes",
)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: str = "error"
    node_key: str = ""
    chunk_index: int | None = None


@dataclass(frozen=True)
class ExplanationCoverage:
    total_nodes: int
    completed_nodes: int
    partial_nodes: int
    failed_nodes: int
    unresolved_external_nodes: int
    truncated: bool
    issues: tuple[ValidationIssue, ...]

    @property
    def status(self) -> str:
        return "completed" if not self.issues and not self.truncated else "partial"

    @property
    def publishable(self) -> bool:
        """Partial candidates remain publishable, but never masquerade as complete."""

        return self.total_nodes > 0 and self.failed_nodes == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_nodes": self.total_nodes,
            "completed_nodes": self.completed_nodes,
            "partial_nodes": self.partial_nodes,
            "failed_nodes": self.failed_nodes,
            "unresolved_external_nodes": self.unresolved_external_nodes,
            "truncated": self.truncated,
            "status": self.status,
        }


def _key(node: Mapping[str, Any]) -> str:
    return str(node.get("node_key") or node.get("id") or node.get("key") or "")


def _is_external(node: Mapping[str, Any]) -> bool:
    return bool(
        node.get("external")
        or node.get("resolvable_source") is False
        or node.get("node_type") in {"external", "boundary", "unresolved"}
        or node.get("type") in {"external", "boundary", "unresolved"}
    )


def _hash_matches(chunk: Mapping[str, Any]) -> bool:
    text = chunk.get("source")
    digest = chunk.get("source_hash")
    if text is None or not digest:
        return bool(digest)  # persisted chunks need not retain sensitive source text
    actual = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
    return actual == str(digest)


def validate_explanation_coverage(
    nodes: Iterable[Mapping[str, Any]],
    chunks: Iterable[Mapping[str, Any]] = (),
    edges: Iterable[Mapping[str, Any]] = (),
    api_explanation: Mapping[str, Any] | None = None,
    *,
    graph_truncated: bool = False,
    truncation_reasons: Iterable[str] = (),
) -> ExplanationCoverage:
    """Validate node, chunk, aggregation, and API traceability coverage.

    Inputs are intentionally dict-shaped so the pure analysis layer does not
    depend on persistence DTOs.  Chunk rows may instead be embedded in a
    node's ``chunks`` list.
    """

    node_rows = list(nodes)
    edge_rows = list(edges)
    api = dict(api_explanation or {})
    issues: list[ValidationIssue] = []
    by_key: dict[str, Mapping[str, Any]] = {}
    external_count = 0
    completed = partial = failed = 0

    for node in node_rows:
        key = _key(node)
        if not key:
            issues.append(ValidationIssue("node_key_missing", "node has no stable key"))
            continue
        if key in by_key:
            issues.append(ValidationIssue("duplicate_node", "node appears more than once", node_key=key))
            continue
        by_key[key] = node
        if _is_external(node):
            external_count += 1
            continue
        status = str(node.get("status") or "missing")
        if status in _SUCCESS:
            completed += 1
        elif status == "failed":
            failed += 1
            issues.append(ValidationIssue("node_failed", "node explanation failed", node_key=key))
        else:
            partial += 1
            issues.append(
                ValidationIssue("node_incomplete", f"node explanation status is {status}", node_key=key)
            )
        if node.get("source_complete") is False:
            issues.append(ValidationIssue("source_incomplete", "node source is incomplete", node_key=key))

        local = node.get("local_explanation") or {}
        aggregate = node.get("aggregate_explanation") or {}
        if not isinstance(local, Mapping) or not local:
            issues.append(ValidationIssue("local_explanation_missing", "local explanation is missing", node_key=key))
        if not isinstance(aggregate, Mapping) or not aggregate:
            issues.append(
                ValidationIssue("aggregate_explanation_missing", "aggregate explanation is missing", node_key=key)
            )
        elif isinstance(local, Mapping):
            for field in _FACT_FIELDS:
                if local.get(field) and not aggregate.get(field):
                    issues.append(
                        ValidationIssue(
                            "aggregate_fact_missing",
                            f"aggregate explanation omits local {field}",
                            node_key=key,
                        )
                    )

    all_chunks = list(chunks)
    for node in node_rows:
        for chunk in node.get("chunks") or ():
            embedded = dict(chunk)
            embedded.setdefault("node_key", _key(node))
            all_chunks.append(embedded)
    chunks_by_node: dict[str, list[Mapping[str, Any]]] = {}
    for chunk in all_chunks:
        key = str(chunk.get("node_key") or "")
        index = chunk.get("chunk_index")
        if key not in by_key:
            issues.append(
                ValidationIssue("chunk_node_unknown", "chunk references an unknown node", node_key=key, chunk_index=index)
            )
            continue
        chunks_by_node.setdefault(key, []).append(chunk)
        start, end = chunk.get("line_start"), chunk.get("line_end")
        if not isinstance(start, int) or not isinstance(end, int) or start > end:
            issues.append(
                ValidationIssue("chunk_range_invalid", "chunk requires a valid inclusive line range", node_key=key, chunk_index=index)
            )
        if not _hash_matches(chunk):
            issues.append(
                ValidationIssue("chunk_hash_invalid", "chunk source hash is missing or incorrect", node_key=key, chunk_index=index)
            )
        if str(chunk.get("status") or "missing") not in _SUCCESS:
            issues.append(
                ValidationIssue("chunk_incomplete", "chunk explanation is incomplete", node_key=key, chunk_index=index)
            )

    for key, node in by_key.items():
        if _is_external(node):
            continue
        expected_chunks = int(node.get("chunks_total") or 0)
        node_chunks = chunks_by_node.get(key, [])
        if expected_chunks and len(node_chunks) != expected_chunks:
            issues.append(
                ValidationIssue(
                    "chunk_count_mismatch",
                    f"expected {expected_chunks} chunks, found {len(node_chunks)}",
                    node_key=key,
                )
            )
        if node_chunks and isinstance(node.get("line_start"), int) and isinstance(node.get("line_end"), int):
            covered: set[int] = set()
            for chunk in node_chunks:
                if isinstance(chunk.get("line_start"), int) and isinstance(chunk.get("line_end"), int):
                    covered.update(range(chunk["line_start"], chunk["line_end"] + 1))
            missing = set(range(node["line_start"], node["line_end"] + 1)) - covered
            if missing:
                issues.append(
                    ValidationIssue(
                        "chunk_line_gap",
                        f"chunks do not cover {len(missing)} source lines",
                        node_key=key,
                    )
                )

    # Every stored edge remains traceable, including unresolved boundary nodes.
    for edge in edge_rows:
        caller = str(edge.get("caller", edge.get("source", "")))
        callee = str(edge.get("callee", edge.get("target", "")))
        if caller not in by_key or callee not in by_key:
            issues.append(
                ValidationIssue("edge_node_unknown", f"edge {caller}->{callee} references an unknown node")
            )

    entry = str(api.get("entry_node_key") or "")
    if not entry or entry not in by_key:
        issues.append(ValidationIssue("entry_node_unknown", "API explanation has no traceable entry node"))
    if not api.get("summary"):
        issues.append(ValidationIssue("api_summary_missing", "API explanation summary is missing"))

    reasons = tuple(str(reason) for reason in truncation_reasons if str(reason))
    truncated = bool(graph_truncated or reasons or any(node.get("truncated") for node in node_rows))
    if truncated:
        issues.append(
            ValidationIssue(
                "analysis_truncated",
                "; ".join(reasons) or "call graph or source analysis was truncated",
            )
        )

    # Preserve first occurrence order while removing identical diagnostics.
    unique = tuple(dict.fromkeys(issues))
    return ExplanationCoverage(
        total_nodes=len(node_rows),
        completed_nodes=completed,
        partial_nodes=partial,
        failed_nodes=failed,
        unresolved_external_nodes=external_count,
        truncated=truncated,
        issues=unique,
    )


class ExplanationCoverageValidator:
    def validate(self, nodes: Iterable[Mapping[str, Any]], **kwargs: Any) -> ExplanationCoverage:
        return validate_explanation_coverage(nodes, **kwargs)
