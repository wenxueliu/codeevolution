"""Manual, bottom-up generation of endpoint explanation snapshots."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict

from ..analysis.knowledge.explanation_graph import build_explanation_plan
from ..analysis.knowledge.explanation_validation import validate_explanation_coverage
from ..analysis.knowledge.source_chunking import chunk_source
from ..domain.explanation import (
    ExplanationChunk,
    ExplanationEdge,
    ExplanationNode,
    ExplanationSnapshot,
)

PROMPT_VERSION = "api-explanation-v1"
SCHEMA_VERSION = "api-explanation-v1"
CHUNKER_VERSION = "semantic-lines-v1"


def _digest(*values) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ExplanationGenerationService:
    """Create and populate one immutable endpoint-scoped explanation snapshot."""

    def __init__(self, store, source_loader, semantic_service, model_id: str):
        self.store = store
        self.source_loader = source_loader
        self.semantic = semantic_service
        self.model_id = model_id

    def prepare(self, spec: dict) -> tuple[str, dict]:
        """Freeze structural input and create a pending candidate without calling the LLM."""
        frozen = self.source_loader.load(spec)
        frozen["_generation_spec"] = dict(spec)
        snapshot_id = uuid.uuid4().hex
        self.store.create_snapshot(
            ExplanationSnapshot(
                id=snapshot_id,
                repo_name=frozen["repo"],
                member_name=frozen["member"],
                api_key=frozen["api_key"],
                method=spec["method"].upper(),
                path=spec["path"],
                handler=spec["handler"],
                entry_node_key=frozen["entry_node_key"],
                source_revision=frozen["source_revision"],
                source_digest=frozen["source_digest"],
                graph_digest=frozen["graph_digest"],
                model_id=self.model_id,
                prompt_version=PROMPT_VERSION,
                schema_version=SCHEMA_VERSION,
            )
        )
        return snapshot_id, frozen

    def generate(self, snapshot_id: str, frozen: dict) -> None:
        """Populate and publish a prepared candidate; safe to run in a worker thread."""
        try:
            self.store.update_snapshot(snapshot_id, "running")
            self._generate(snapshot_id, frozen)
        except Exception as error:
            current = self.store.get_snapshot(snapshot_id)
            if current and current.status not in {"completed", "partial", "failed", "cancelled"}:
                self.store.update_snapshot(snapshot_id, "failed", error=str(error)[:1000])

    def _generate(self, snapshot_id: str, frozen: dict) -> None:
        nodes: dict[str, dict] = frozen["nodes"]
        edges: list[dict] = frozen["edges"]
        plan = build_explanation_plan(
            nodes.keys(),
            edges,
            truncated=frozen.get("truncated", False),
            truncation_reasons=("call graph limit reached",) if frozen.get("truncated") else (),
        )
        current = self.store.get_current(frozen["repo"], frozen["member"], frozen["api_key"])
        reusable = {
            node.node_key: node for node in self.store.list_nodes(current.id)
        } if current and self._compatible(current) else {}

        edge_by_parent: dict[str, list[dict]] = {node_id: [] for node_id in nodes}
        for edge in edges:
            edge_by_parent.setdefault(edge["source"], []).append(edge)
            self.store.save_edge(
                ExplanationEdge(
                    snapshot_id=snapshot_id,
                    caller_key=nodes[edge["source"]]["node_key"],
                    callee_key=nodes[edge["target"]]["node_key"],
                    call_site=edge.get("call_site") or {},
                )
            )

        local_by_id: dict[str, dict] = {}
        local_digest_by_id: dict[str, str] = {}
        chunks_by_id: dict[str, list[dict]] = {}
        statuses: dict[str, str] = {}
        model_calls = 0
        reused_local = 0

        # Local explanations are independent and must all exist before an SCC
        # can aggregate its internal cycle references.
        for node_id in plan.node_order:
            if self.store.is_cancelled(snapshot_id):
                return
            node = nodes[node_id]
            local_digest = _digest(
                node["source_hash"], self.model_id, PROMPT_VERSION, SCHEMA_VERSION, CHUNKER_VERSION
            )
            local_digest_by_id[node_id] = local_digest
            previous = reusable.get(node["node_key"])
            if previous and previous.local_digest == local_digest:
                local_by_id[node_id] = previous.local_explanation
                old_chunks = self.store.list_chunks(current.id, node["node_key"])
                chunk_rows = [
                    {
                        "chunk_index": chunk.chunk_index,
                        "line_start": chunk.line_start,
                        "line_end": chunk.line_end,
                        "source_hash": chunk.source_hash,
                        "explanation": chunk.explanation,
                        "status": "reused",
                    }
                    for chunk in old_chunks
                ]
                reused_local += 1
            else:
                chunking = chunk_source(
                    node["source"],
                    line_start=node["line_start"],
                    expected_line_end=node["line_end"],
                )
                if not chunking.chunks:
                    local_by_id[node_id] = {}
                    chunk_rows = []
                    statuses[node_id] = "partial"
                else:
                    chunk_rows = []
                    for chunk in chunking.chunks:
                        explanation = self.semantic.explain_chunk(node, asdict(chunk))
                        model_calls += 1
                        chunk_rows.append(
                            {
                                **asdict(chunk),
                                "explanation": explanation,
                                "status": "completed",
                            }
                        )
                    local_by_id[node_id] = self.semantic.synthesize_local(node, chunk_rows)
                    if len(chunk_rows) > 1:
                        model_calls += 1
                    statuses[node_id] = "completed" if chunking.source_complete else "partial"
            chunks_by_id[node_id] = chunk_rows
            statuses.setdefault(node_id, "reused" if previous and previous.local_digest == local_digest else "completed")
            self.store.save_node(
                ExplanationNode(
                    snapshot_id=snapshot_id,
                    node_key=node["node_key"],
                    source_hash=node["source_hash"],
                    local_digest=local_digest,
                    aggregate_digest="",
                    local_explanation=local_by_id[node_id],
                    status="running",
                )
            )
            for chunk in chunk_rows:
                self.store.save_chunk(
                    ExplanationChunk(
                        snapshot_id=snapshot_id,
                        node_key=node["node_key"],
                        chunk_index=chunk["chunk_index"],
                        line_start=chunk["line_start"],
                        line_end=chunk["line_end"],
                        source_hash=chunk["source_hash"],
                        explanation=chunk["explanation"],
                        status=chunk["status"],
                    )
                )

        aggregate_by_id: dict[str, dict] = {}
        aggregate_digest_by_id: dict[str, str] = {}
        component_by_node = plan.component_by_node
        for node_id in plan.node_order:
            if self.store.is_cancelled(snapshot_id):
                return
            node = nodes[node_id]
            child_rows = []
            child_digests = []
            for edge in edge_by_parent.get(node_id, []):
                child_id = edge["target"]
                same_cycle = component_by_node[node_id] == component_by_node[child_id]
                child_aggregate = (
                    local_by_id[child_id]
                    if same_cycle
                    else aggregate_by_id.get(child_id, local_by_id[child_id])
                )
                child_rows.append(
                    {
                        "node_key": nodes[child_id]["node_key"],
                        "call_line": edge.get("call_line"),
                        "aggregate": child_aggregate,
                    }
                )
                child_digests.append(
                    aggregate_digest_by_id.get(child_id, local_digest_by_id[child_id])
                )
            aggregate_digest = _digest(
                local_digest_by_id[node_id],
                sorted(child_digests),
                [(edge["target"], edge.get("call_line")) for edge in edge_by_parent.get(node_id, [])],
                PROMPT_VERSION,
            )
            aggregate_digest_by_id[node_id] = aggregate_digest
            previous = reusable.get(node["node_key"])
            if previous and previous.aggregate_digest == aggregate_digest:
                aggregate = previous.aggregate_explanation
            else:
                aggregate = self.semantic.aggregate_node(node, local_by_id[node_id], child_rows)
                if child_rows:
                    model_calls += 1
            aggregate_by_id[node_id] = aggregate
            self.store.save_node(
                ExplanationNode(
                    snapshot_id=snapshot_id,
                    node_key=node["node_key"],
                    source_hash=node["source_hash"],
                    local_digest=local_digest_by_id[node_id],
                    aggregate_digest=aggregate_digest,
                    local_explanation=local_by_id[node_id],
                    aggregate_explanation=aggregate,
                    status=statuses[node_id],
                )
            )

        root_id = frozen["entry_id"]
        api_explanation = {
            **aggregate_by_id[root_id],
            "method": self.store.get_snapshot(snapshot_id).method,
            "path": self.store.get_snapshot(snapshot_id).path,
            "entry_node_key": frozen["entry_node_key"],
        }
        validation_nodes = []
        validation_chunks = []
        for node_id, node in nodes.items():
            validation_nodes.append(
                {
                    **node,
                    "status": statuses[node_id],
                    "local_explanation": local_by_id[node_id],
                    "aggregate_explanation": aggregate_by_id[node_id],
                    "chunks_total": len(chunks_by_id[node_id]),
                    "source_complete": statuses[node_id] != "partial",
                }
            )
            validation_chunks.extend(
                {**chunk, "node_key": node["node_key"]} for chunk in chunks_by_id[node_id]
            )
        validation_edges = [
            {
                "caller": nodes[edge["source"]]["node_key"],
                "callee": nodes[edge["target"]]["node_key"],
            }
            for edge in edges
        ]
        self.store.update_snapshot(snapshot_id, "validating")
        coverage = validate_explanation_coverage(
            validation_nodes,
            validation_chunks,
            validation_edges,
            api_explanation,
            graph_truncated=frozen.get("truncated", False),
            truncation_reasons=("call graph limit reached",) if frozen.get("truncated") else (),
        )
        coverage_data = coverage.as_dict()
        coverage_data["unresolved_external_nodes"] = frozen.get("unresolved_external_nodes", 0)
        coverage_data["issues"] = [asdict(issue) for issue in coverage.issues]
        statistics = {
            "model_calls": model_calls,
            "reused_local_nodes": reused_local,
            "shared_nodes": len(plan.shared_nodes),
            "components": len(plan.components),
        }
        if not coverage.publishable:
            self.store.update_snapshot(
                snapshot_id,
                "failed",
                explanation=api_explanation,
                coverage=coverage_data,
                statistics=statistics,
                error="解释覆盖校验失败",
            )
            return
        latest = self.source_loader.load(frozen["_generation_spec"])
        if (
            latest["source_digest"] != frozen["source_digest"]
            or latest["graph_digest"] != frozen["graph_digest"]
        ):
            coverage_data["outdated_during_generation"] = True
            self.store.update_snapshot(
                snapshot_id,
                "failed",
                explanation=api_explanation,
                coverage=coverage_data,
                statistics=statistics,
                error="生成期间源码或调用图发生变化，候选快照未发布",
            )
            return
        final_status = coverage.status
        self.store.update_snapshot(
            snapshot_id,
            final_status,
            explanation=api_explanation,
            coverage=coverage_data,
            statistics=statistics,
        )
        self.store.publish(snapshot_id)

    def _compatible(self, snapshot) -> bool:
        return (
            snapshot.model_id == self.model_id
            and snapshot.prompt_version == PROMPT_VERSION
            and snapshot.schema_version == SCHEMA_VERSION
        )
