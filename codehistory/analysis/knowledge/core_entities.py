"""Core-entity identification using call-graph centrality."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import networkx as nx

from ...domain.knowledge import CoreEntity, FunctionDef


class _Source(Protocol):
    def call_edges(self) -> list[dict[str, Any]]: ...
    def get_function_by_id(self, node_id: str) -> FunctionDef | None: ...


class CoreEntityExtractor:
    def __init__(
        self,
        source: _Source | Callable[[], list[CoreEntity]],
        classify_layer: Callable[[str], str] | None = None,
    ):
        self._source = source
        self._classify_layer = classify_layer or (lambda _path: "")
        self._call_graph: nx.DiGraph | None = None

    def extract(self, top_n: int = 30) -> list[CoreEntity]:
        if callable(self._source) and not hasattr(self._source, "call_edges"):
            return self._source()
        graph = self.call_graph()
        if graph.number_of_nodes() == 0:
            return []
        ranked = sorted(self.pagerank(graph).items(), key=lambda item: -item[1])[:top_n]
        entities = []
        for node_id, score in ranked:
            function = self._source.get_function_by_id(node_id)  # type: ignore[union-attr]
            if function is None:
                continue
            entities.append(
                CoreEntity(
                    node_id=node_id,
                    name=function.name,
                    qualified_name=function.qualified_name,
                    file_path=function.file_path,
                    kind=function.kind,
                    pagerank=round(score, 6),
                    in_degree=graph.in_degree(node_id),
                    out_degree=graph.out_degree(node_id),
                    layer=self._classify_layer(function.file_path),
                )
            )
        return entities

    def call_graph(self) -> nx.DiGraph:
        if self._call_graph is None:
            graph = nx.DiGraph()
            for row in self._source.call_edges():  # type: ignore[union-attr]
                graph.add_edge(row["source"], row["target"])
            self._call_graph = graph
        return self._call_graph

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
