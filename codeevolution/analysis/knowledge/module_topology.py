"""Module-topology extraction independent from the legacy facade."""

from __future__ import annotations

from collections import Counter, defaultdict
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
                    "name": self.module_name(files),
                }
            )
        self._dedupe_names(modules)

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

    # Louvain clusters by coupling, so a community often spans several
    # directories and a strict common prefix collapses to "root" — which
    # collides across modules. Naming falls back to the directory that
    # covers the most files, then to an explicit "mixed" label.
    DOMINANT_DIR_MIN_COVERAGE = 0.5
    MIXED_TOP_DIRS = 3

    @classmethod
    def module_name(cls, paths: list[str]) -> str:
        if not paths:
            return "root"
        directories = [Path(path).parent for path in paths]
        prefix = cls._common_directory_prefix(directories)
        if prefix:
            return prefix
        dominant = cls._dominant_directory(directories)
        if dominant:
            return dominant
        return cls._mixed_label(directories)

    @staticmethod
    def _common_directory_prefix(directories: list[Path]) -> str:
        parts_list = [directory.parts for directory in directories]
        prefix = []
        for index in range(min(len(parts) for parts in parts_list)):
            segment = parts_list[0][index]
            if not all(parts[index] == segment for parts in parts_list):
                break
            prefix.append(segment)
        return "/".join(prefix)

    @classmethod
    def _dominant_directory(cls, directories: list[Path]) -> str:
        """Deepest ancestor directory covering >= half the community's files.

        Preferring the *deepest* eligible directory (rather than the widest)
        keeps different communities under the same parent distinguishable.
        """
        if not directories:
            return ""
        coverage: Counter[str] = Counter()
        depth: dict[str, int] = {}
        for directory in directories:
            seen: set[str] = set()
            for ancestor in [directory, *directory.parents]:
                text = ancestor.as_posix()
                if text == ".":
                    # The repo root is an ancestor of everything; counting it
                    # would always win, so it carries no naming signal.
                    continue
                if text in seen:
                    continue
                seen.add(text)
                coverage[text] += 1
                depth[text] = len(ancestor.parts)
        if not coverage:
            return ""
        threshold = len(directories) * cls.DOMINANT_DIR_MIN_COVERAGE
        candidates = [name for name, count in coverage.items() if count >= threshold]
        if not candidates:
            return ""
        # Deeper path is more specific; then more files; then lexicographic
        # order keeps it deterministic.
        return max(candidates, key=lambda name: (depth[name], coverage[name], name))

    @classmethod
    def _mixed_label(cls, directories: list[Path]) -> str:
        tops: Counter[str] = Counter()
        for directory in directories:
            first = directory.parts[0] if directory.parts else "."
            tops["root" if first == "." else first] += 1
        if set(tops) == {"root"}:
            return "root"
        top = sorted(tops.items(), key=lambda item: (-item[1], item[0]))
        return "mixed:" + "+".join(name for name, _ in top[: cls.MIXED_TOP_DIRS])

    @classmethod
    def _dedupe_names(cls, modules: list[dict]) -> None:
        """Disambiguate colliding module names.

        First try a directory segment from the community that the name does
        not already contain (``src`` vs ``src/views``); numeric suffixes are
        the last resort. Modules are ordered by size, so larger communities
        get first pick.
        """
        groups: dict[str, list[dict]] = defaultdict(list)
        for module in modules:
            groups[module["name"]].append(module)
        for name, group in groups.items():
            if len(group) > 1:
                for module in group:
                    segment = cls._discriminator(module["files"], name)
                    if segment:
                        module["name"] = f"{name}/{segment}"
        seen: Counter[str] = Counter()
        for module in modules:
            name = module["name"]
            seen[name] += 1
            if seen[name] > 1:
                module["name"] = f"{name}-{seen[name]}"

    @staticmethod
    def _discriminator(files: list[str], name: str) -> str:
        """Most common directory segment of the community's files that the
        name does not already mention."""
        excluded = {part for chunk in name.replace(":", "/").split("+") for part in chunk.split("/")}
        counts: Counter[str] = Counter()
        for file_path in files:
            for part in Path(file_path).parent.parts:
                if part not in excluded:
                    counts[part] += 1
        if not counts:
            return ""
        return min(counts, key=lambda part: (-counts[part], part))
