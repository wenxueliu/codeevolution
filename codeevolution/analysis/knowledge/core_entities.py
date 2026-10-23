"""Domain-entity identification using type structure and relationships."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import networkx as nx

from ...domain.knowledge import CoreEntity
from .entity_profiles import EntityProfile, first_match, profile_for_path, skipped


class _Source(Protocol):
    def domain_type_nodes(self) -> list[dict[str, Any]]: ...
    def domain_type_relationships(self) -> list[dict[str, Any]]: ...
    def domain_type_field_counts(self) -> list[dict[str, Any]]: ...
    def domain_type_fields(self, node_id: str) -> list[dict[str, Any]]: ...


class CoreEntityExtractor:
    def __init__(
        self,
        source: _Source | Callable[[], list[CoreEntity]],
        classify_layer: Callable[[str], str] | None = None,
        profile_resolver: Callable[[str], EntityProfile] | None = None,
    ):
        self._source = source
        self._classify_layer = classify_layer or (lambda _path: "")
        self._profile_for_path = profile_resolver or profile_for_path
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
        scored = []
        for item in nodes:
            node_id = item["id"]
            profile = self._profile_for_path(item.get("file_path", ""))
            if skipped(item.get("file_path", ""), profile):
                continue
            breakdown = self.score_details(
                item,
                centrality.get(node_id, 0),
                field_counts.get(node_id, 0),
                graph.degree(node_id),
                profile,
            )
            if breakdown["total"] <= 0:
                continue
            scored.append((breakdown["total"], breakdown, profile, item))
        scored.sort(key=lambda row: -row[0])
        entities = []
        for score, breakdown, profile, item in scored[:top_n]:
            node_id = item["id"]
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
                    start_line=item.get("start_line", 0),
                    fields=self._source.domain_type_fields(node_id) if hasattr(self._source, "domain_type_fields") else [],
                    score=round(score, 4),
                    annotations=self.decode_annotations(item.get("decorators")),
                    language=profile.key,
                    score_breakdown={key: round(value, 4) for key, value in breakdown.items()},
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
    def domain_score(
        item: dict,
        centrality: float,
        fields: int,
        relationships: int,
        profile: EntityProfile | None = None,
    ) -> float:
        return CoreEntityExtractor.score_details(
            item, centrality, fields, relationships, profile
        )["total"]

    @staticmethod
    def score_details(
        item: dict,
        centrality: float,
        fields: int,
        relationships: int,
        profile: EntityProfile | None = None,
    ) -> dict[str, float]:
        """Explainable entity score: graph signal plus language-specific priors."""
        profile = profile or profile_for_path(item.get("file_path", ""))
        path = (item.get("file_path") or "").lower().replace("\\", "/")
        name = (item.get("name") or "").lower()
        annotations = " ".join(CoreEntityExtractor.decode_annotations(item.get("decorators"))).lower()
        kind = str(item.get("kind") or "").lower()

        path_bonus = first_match(path, profile.path_bonuses)[1]
        path_penalty = first_match(path, profile.path_penalties)[1]
        annotation_bonus = first_match(annotations, profile.annotation_bonuses)[1]
        name_penalty = next(
            (delta for suffix, delta in profile.name_suffix_penalties if name.endswith(suffix)),
            0.0,
        )
        kind_penalty = next(
            (delta for token, delta in profile.kind_penalties if kind == token),
            0.0,
        )
        breakdown = {
            "pagerank": centrality * 30,
            "fields": min(fields, 20) * 0.8,
            "relationships": min(relationships, 20) * 0.5,
            "path_bonus": path_bonus,
            "path_penalty": -path_penalty,
            "annotation_bonus": annotation_bonus,
            "name_penalty": -name_penalty,
            "kind_penalty": -kind_penalty,
        }
        breakdown["total"] = sum(breakdown.values())
        return breakdown

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
