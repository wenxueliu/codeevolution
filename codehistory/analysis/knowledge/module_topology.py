"""Module-topology extraction independent from the legacy facade."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import networkx as nx
from networkx.algorithms.community import louvain_communities

from ...domain.knowledge import ModuleTopology


class _QuerySource(Protocol):
    def module_import_edges(self) -> list[dict[str, Any]]: ...
    def cross_file_call_edges(self) -> list[dict[str, Any]]: ...


class ModuleTopologyExtractor:
    """Detect modules and their dependencies from imports and calls."""

    def __init__(self, source: _QuerySource | Callable[[], ModuleTopology]):
        self._source = source

    def extract(self, resolution: float = 0.8) -> ModuleTopology:
        if callable(self._source) and not hasattr(self._source, "module_import_edges"):
            return self._source()

        graph = self.build_graph()
        if graph.number_of_edges() == 0:
            return ModuleTopology()

        communities = louvain_communities(graph, resolution=resolution, seed=42)
        modules = []
        file_to_module: dict[str, int] = {}
        for index, community in enumerate(communities):
            files = sorted(community)
            language_counts: dict[str, int] = defaultdict(int)
            for file_path in files:
                file_to_module[file_path] = index
                language_counts[Path(file_path).suffix.lower()] += 1
            primary_language = (
                max(language_counts, key=language_counts.get) if language_counts else ""
            )
            modules.append(
                {
                    "id": f"mod-{index + 1}",
                    "files": files,
                    "file_count": len(files),
                    "primary_language": primary_language,
                    "name": self.common_prefix(files),
                }
            )

        dependencies: dict[str, set[str]] = defaultdict(set)
        inter_module_edges = 0
        for source, target in graph.edges():
            source_module = file_to_module.get(source)
            target_module = file_to_module.get(target)
            if source_module is not None and target_module is not None and source_module != target_module:
                dependencies[f"mod-{source_module + 1}"].add(f"mod-{target_module + 1}")
                inter_module_edges += 1

        return ModuleTopology(
            modules=sorted(modules, key=lambda module: -module["file_count"]),
            dependency_graph={key: sorted(value) for key, value in dependencies.items()},
            coupling_score=round(inter_module_edges / max(graph.number_of_edges(), 1), 4),
        )

    def build_graph(self) -> nx.Graph:
        graph = nx.Graph()
        imports = self._source.module_import_edges()  # type: ignore[union-attr]
        self._add_edges(graph, imports, weight=2)

        calls = self._source.cross_file_call_edges()  # type: ignore[union-attr]
        self._add_edges(graph, calls, weight=1)
        return graph

    @staticmethod
    def _add_edges(graph: nx.Graph, rows: list[dict[str, Any]], weight: int) -> None:
        for row in rows:
            source, target = row["f1"], row["f2"]
            if source == target:
                continue
            if graph.has_edge(source, target):
                graph[source][target]["weight"] += weight
            else:
                graph.add_edge(source, target, weight=weight)

    @staticmethod
    def common_prefix(paths: list[str]) -> str:
        if not paths:
            return ""
        directories = [Path(path).parent.parts for path in paths]
        prefix = []
        for index in range(min(len(directory) for directory in directories)):
            segment = directories[0][index]
            if not all(directory[index] == segment for directory in directories):
                break
            prefix.append(segment)
        return "/".join(prefix) if prefix else "root"
