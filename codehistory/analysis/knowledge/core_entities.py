"""Domain-entity identification using type structure and relationships."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import networkx as nx

from ...domain.knowledge import CoreEntity


class _Source(Protocol):
    def domain_type_nodes(self) -> list[dict[str, Any]]: ...
    def domain_type_relationships(self) -> list[dict[str, Any]]: ...
    def domain_type_field_counts(self) -> list[dict[str, Any]]: ...


class CoreEntityExtractor:
    def __init__(
        self,
        source: _Source | Callable[[], list[CoreEntity]],
        classify_layer: Callable[[str], str] | None = None,
    ):
        self._source = source
        self._classify_layer = classify_layer or (lambda _path: "")
        self._type_graph: nx.DiGraph | None = None

    def extract(self, top_n: int = 30) -> list[CoreEntity]:
        if callable(self._source) and not hasattr(self._source, "call_edges"):
            return self._source()
        nodes = self._source.domain_type_nodes()  # type: ignore[union-attr]
        if not nodes:
            return []
        graph = self.type_graph(nodes)
        centrality = self.pagerank(graph)
        field_counts = {
            row["id"]: int(row["field_count"])
            for row in self._source.domain_type_field_counts()  # type: ignore[union-attr]
        }
        ranked = sorted(
            nodes,
            key=lambda item: -self.domain_score(
                item,
                centrality.get(item["id"], 0),
                field_counts.get(item["id"], 0),
                graph.degree(item["id"]),
            ),
        )[:top_n]
        entities = []
        for item in ranked:
            node_id = item["id"]
            score = self.domain_score(
                item,
                centrality.get(node_id, 0),
                field_counts.get(node_id, 0),
                graph.degree(node_id),
            )
            entities.append(
                CoreEntity(
                    node_id=node_id,
                    name=item["name"],
                    qualified_name=item["qualified_name"],
                    file_path=item["file_path"],
                    kind=item["kind"],
                    pagerank=round(centrality.get(node_id, 0), 6),
                    in_degree=graph.in_degree(node_id),
                    out_degree=graph.out_degree(node_id),
                    layer=self._classify_layer(item["file_path"]),
                    field_count=field_counts.get(node_id, 0),
                    relationship_count=graph.degree(node_id),
                    score=round(score, 4),
                    annotations=self.decode_annotations(item.get("decorators")),
                )
            )
        return entities

    def type_graph(self, nodes: list[dict[str, Any]] | None = None) -> nx.DiGraph:
        if self._type_graph is None:
            graph = nx.DiGraph()
            for item in nodes or self._source.domain_type_nodes():  # type: ignore[union-attr]
                graph.add_node(item["id"])
            for row in self._source.domain_type_relationships():  # type: ignore[union-attr]
                graph.add_edge(row["source"], row["target"])
            self._type_graph = graph
        return self._type_graph

    def call_graph(self) -> nx.DiGraph:
        """Compatibility alias retained for callers of the old implementation detail."""
        return self.type_graph()

    @staticmethod
    def domain_score(item: dict, centrality: float, fields: int, relationships: int) -> float:
        path = item["file_path"].lower()
        name = item["name"].lower()
        annotations = " ".join(CoreEntityExtractor.decode_annotations(item.get("decorators"))).lower()
        score = centrality * 30 + min(fields, 20) * 0.8 + min(relationships, 20) * 0.5
        if any(part in path for part in ("/domain/", "/model/", "/entity/", "/pojo/")):
            score += 8
        if "/types/" in path:
            score += 4
        if path.startswith(("public/", "node_modules/")) or ".d.ts" in path:
            score -= 14
        if any(part in path for part in ("/controller/", "/service/", "/config/")):
            score -= 6
        if any(token in annotations for token in ("entity", "document", "table", "aggregate")):
            score += 10
        if name.endswith(("controller", "service", "repository", "config", "test")):
            score -= 8
        return score

    @staticmethod
    def decode_annotations(raw: Any) -> list[str]:
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(item) for item in raw]
        import json

        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return [str(raw)]
        return [str(item) for item in value] if isinstance(value, list) else []

    @staticmethod
    def pagerank(
        graph: nx.DiGraph, alpha: float = 0.85, max_iter: int = 100, tol: float = 1e-6
    ) -> dict[str, float]:
        nodes = list(graph.nodes())
        if not nodes:
            return {}
        count = len(nodes)
        out_degree = {node: max(graph.out_degree(node), 1) for node in nodes}
        rank = {node: 1.0 / count for node in nodes}
        for _ in range(max_iter):
            previous = dict(rank)
            dangling = alpha * sum(
                previous[node] for node in nodes if graph.out_degree(node) == 0
            ) / count
            for node in nodes:
                rank[node] = dangling + (1.0 - alpha) / count
                for predecessor in graph.predecessors(node):
                    rank[node] += alpha * previous[predecessor] / out_degree[predecessor]
            if sum(abs(rank[node] - previous[node]) for node in nodes) < tol * count:
                break
        return rank
