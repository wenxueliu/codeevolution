"""Module topology: a deterministic path skeleton annotated with coupling.

Two different questions need two different signals, and conflating them was
the core mistake of the previous implementation:

* **"Which files form a module?"** is a *structural* question. Build manifests
  (``pom.xml``/``package.json``/``go.mod``/...) plus the directory layout answer
  it exactly and reproducibly for any well-designed repository.
* **"How do modules depend on each other?"** is a *coupling* question. Only the
  import/call graph can answer it.

So the skeleton (modules, parents, names, depth) is derived from paths, while
the edges, ``coupling_score`` and the per-module coupling metrics come from the
graph: ``dependency_graph`` keeps the edge direction (``A -> B`` means A imports
or calls B), ``afferent``/``efferent`` count the modules pointing at and away
from each module, and ``cohesion`` is the share of a module's edges that stay
inside it. Low cohesion is precisely where the layout and the real coupling
disagree, and that disagreement is surfaced as a metric instead of being
smuggled into a module name (the old ``mixed:a+b+c`` labels).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any, Protocol

import networkx as nx

from ...domain.knowledge import ModuleTopology


class _QuerySource(Protocol):
    def module_import_edges(self) -> list[dict[str, Any]]: ...
    def cross_file_call_edges(self) -> list[dict[str, Any]]: ...


# Files that mark a module root in their ecosystem's build system.
_MANIFEST_NAMES = frozenset(
    {
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "package.json",
        "go.mod",
        "cargo.toml",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pubspec.yaml",
        "build.sbt",
        "composer.json",
        "gemfile",
        "mix.exs",
        "cmakelists.txt",
    }
)
_MANIFEST_SUFFIXES = (".csproj", ".sln", ".gemspec", ".podspec")

# Directories holding vendored, generated or build output rather than modules.
_NON_SOURCE_DIRS = frozenset(
    {
        "node_modules",
        "dist",
        "build",
        "out",
        "target",
        "vendor",
        "third_party",
        "thirdparty",
        "external",
        "public",
        "static",
        "site-packages",
        "bower_components",
        "jspm_packages",
        "__pycache__",
        ".venv",
        "venv",
        "coverage",
        ".git",
        ".idea",
        ".vscode",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    }
)

# File kinds that never define a code module.
_NON_SOURCE_SUFFIXES = (
    ".md",
    ".rst",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".toml",
    ".ini",
    ".properties",
    ".csv",
    ".tsv",
    ".lock",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".map",
    ".d.ts",
    ".min.js",
    ".min.css",
)

_NON_SOURCE_LANGUAGES = frozenset(
    {
        "xml",
        "yaml",
        "yml",
        "json",
        "toml",
        "ini",
        "properties",
        "markdown",
        "md",
        "csv",
        "text",
        "txt",
        "plaintext",
        "dockerfile",
        "makefile",
        "lock",
        "svg",
    }
)

# Container directories that carry no module meaning once the module root is
# known: the Java/Kotlin/Maven source root, and the ubiquitous ``src``/``lib``.
_CONTAINER_PARTS = frozenset(
    {"src", "main", "test", "tests", "java", "kotlin", "resources", "source", "sources"}
)

_REPO_ROOT = PurePosixPath(".")
_ROOT_ID = "."


class ModuleTopologyExtractor:
    """Derive a module tree from the layout, then annotate it with coupling."""

    # How many path levels below a module root become separate sub-modules.
    MAX_SUB_DEPTH = 2

    def __init__(self, source: _QuerySource | Callable[[], ModuleTopology]):
        self._source = source

    def extract(self, resolution: float = 0.8, max_sub_depth: int | None = None) -> ModuleTopology:
        """Build the module topology.

        ``resolution`` is accepted for backward compatibility with the former
        Louvain clustering and no longer affects the result.
        """
        if callable(self._source) and not hasattr(self._source, "module_import_edges"):
            return self._source()

        output_depth = self.MAX_SUB_DEPTH if max_sub_depth is None else max_sub_depth
        imports, calls = self._edge_rows()
        graph = self._graph_from_rows(imports, calls)
        records = self._file_records()
        all_paths = [path for path, _ in records] or [str(node) for node in graph.nodes()]
        if not all_paths:
            return ModuleTopology()

        source_records = self._source_records(records, all_paths)
        modules, meta, leaf_of = self._layout(source_records, all_paths, output_depth)
        if not modules:
            return ModuleTopology()

        self._annotate_coupling(modules, meta, leaf_of, imports, calls)
        modules.sort(key=lambda module: (module["depth"], module["id"]))
        return ModuleTopology(
            modules=modules,
            dependency_graph=meta["_dependencies"],
            coupling_score=meta["_coupling"],
        )

    # ------------------------------------------------------------------ graph

    def _edge_rows(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Raw directed edge rows: ``f1`` depends on ``f2``."""
        imports = self._source.module_import_edges() or []  # type: ignore[union-attr]
        calls = self._source.cross_file_call_edges() or []  # type: ignore[union-attr]
        return imports, calls

    def build_graph(self) -> nx.Graph:
        imports, calls = self._edge_rows()
        return self._graph_from_rows(imports, calls)

    @classmethod
    def _graph_from_rows(cls, imports: list[dict], calls: list[dict]) -> nx.Graph:
        graph = nx.Graph()
        cls._add_edges(graph, imports, weight=2)
        cls._add_edges(graph, calls, weight=1)
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

    # ------------------------------------------------------------- file input

    def _file_records(self) -> list[tuple[str, str]]:
        getter = getattr(self._source, "file_records", None)
        if not callable(getter):
            return []
        rows = getter() or []
        return [
            (str(row.get("path") or row.get("file_path") or ""), str(row.get("language") or ""))
            for row in rows
        ]

    def _source_records(
        self, records: list[tuple[str, str]], all_paths: list[str]
    ) -> list[tuple[str, str]]:
        candidates = [
            (path, language) for path, language in records if self.is_source_file(path, language)
        ]
        if candidates:
            return candidates
        return [(path, self._language_of(path)) for path in all_paths if self.is_source_file(path)]

    @classmethod
    def is_source_file(cls, path: str, language: str = "") -> bool:
        lowered = path.lower()
        if any(lowered.endswith(suffix) for suffix in _NON_SOURCE_SUFFIXES):
            return False
        parts = PurePosixPath(lowered).parts
        if any(part in _NON_SOURCE_DIRS for part in parts[:-1]):
            return False
        if any(part.startswith(".") for part in parts[:-1]):
            return False
        if language and language.strip().lower() in _NON_SOURCE_LANGUAGES:
            return False
        return True

    @staticmethod
    def _language_of(path: str) -> str:
        return PurePosixPath(path).suffix.lstrip(".").lower()

    # ------------------------------------------------------------ path layout

    @classmethod
    def module_roots(cls, paths: list[str]) -> list[PurePosixPath]:
        """Directories that declare themselves a build unit.

        Only the *innermost* declarations count, so a Maven aggregator root is
        replaced by its child modules while a single-module project keeps its
        own root.
        """
        found: set[PurePosixPath] = set()
        for path in paths:
            name = PurePosixPath(path).name.lower()
            if name in _MANIFEST_NAMES or name.endswith(_MANIFEST_SUFFIXES):
                found.add(PurePosixPath(path).parent)
        if not found:
            return [_REPO_ROOT]
        leaves = sorted(
            (
                candidate
                for candidate in found
                if not any(
                    other != candidate and other != _REPO_ROOT and other.is_relative_to(candidate)
                    for other in found
                )
            ),
            key=lambda candidate: candidate.as_posix(),
        )
        return leaves or [_REPO_ROOT]

    @classmethod
    def _normalise(cls, parts: tuple[str, ...]) -> tuple[str, ...]:
        """Drop the leading container run (``src/main/java`` and friends).

        A file sitting directly in a container directory (``src/App.vue``)
        keeps its segment instead of collapsing onto the repo root.
        """
        index = 0
        while index < len(parts) and parts[index].lower() in _CONTAINER_PARTS:
            index += 1
        stripped = parts[index:]
        return stripped or parts

    @staticmethod
    def _common_prefix(sequences: list[tuple[str, ...]]) -> tuple[str, ...]:
        if not sequences:
            return ()
        length = min(len(sequence) for sequence in sequences)
        prefix: list[str] = []
        for index in range(length):
            segment = sequences[0][index]
            if not all(sequence[index] == segment for sequence in sequences):
                break
            prefix.append(segment)
        return tuple(prefix)

    def _layout(
        self, source_records: list[tuple[str, str]], all_paths: list[str], output_depth: int
    ) -> tuple[list[dict], dict, dict[str, str]]:
        roots = self.module_roots(all_paths)
        ordered_roots = sorted(roots, key=lambda root: len(root.parts), reverse=True)

        entries: list[tuple[str, str, PurePosixPath, tuple[str, ...]]] = []
        for path, language in source_records:
            pure = PurePosixPath(path)
            root = next(
                (
                    candidate
                    for candidate in ordered_roots
                    if candidate == _REPO_ROOT or pure.is_relative_to(candidate)
                ),
                _REPO_ROOT,
            )
            relative = pure if root == _REPO_ROOT else pure.relative_to(root)
            entries.append((path, language, root, self._normalise(relative.parent.parts)))

        common = self._common_prefix([segments for _, _, _, segments in entries if segments])

        owner: dict[str, list[str]] = defaultdict(list)
        languages: dict[str, Counter[str]] = defaultdict(Counter)
        leaf_of: dict[str, str] = {}
        for path, language, root, segments in entries:
            tail = segments[len(common) :]
            module_id = self._module_id(root, tail[:output_depth])
            owner[module_id].append(path)
            languages[module_id][language or self._language_of(path)] += 1
            leaf_of[path] = module_id

        meta: dict[str, Any] = {"_dependencies": {}, "_coupling": 0.0}
        for module_id in list(owner):
            self._ensure_ancestors(meta, module_id)
        meta.setdefault(_ROOT_ID, self._module_meta(_ROOT_ID))

        modules = []
        for module_id, node in meta.items():
            if module_id.startswith("_"):
                continue
            node["files"] = sorted(owner.get(module_id, []))
            node["file_count"] = len(node["files"])
            node["total_file_count"] = self._subtree_file_count(module_id, owner)
            node["primary_language"] = self._primary_language(module_id, languages)
            modules.append(node)
        return modules, meta, leaf_of

    @staticmethod
    def _module_id(root: PurePosixPath, segments: tuple[str, ...]) -> str:
        prefix = "" if root == _REPO_ROOT else root.as_posix()
        parts = [part for part in (prefix, *segments) if part]
        return "/".join(parts) or _ROOT_ID

    def _ensure_ancestors(self, meta: dict[str, Any], module_id: str) -> None:
        parent = self._parent_id(module_id)
        while parent is not None and parent not in meta:
            meta[parent] = self._module_meta(parent)
            parent = self._parent_id(parent)
        if module_id not in meta:
            meta[module_id] = self._module_meta(module_id)

    @staticmethod
    def _parent_id(module_id: str) -> str | None:
        if module_id == _ROOT_ID:
            return None
        if "/" in module_id:
            return module_id.rsplit("/", 1)[0]
        return _ROOT_ID

    @staticmethod
    def _module_meta(module_id: str) -> dict:
        return {
            "id": module_id,
            "name": "root" if module_id == _ROOT_ID else module_id,
            "segment": "root" if module_id == _ROOT_ID else module_id.rsplit("/", 1)[-1],
            "path": "" if module_id == _ROOT_ID else module_id,
            "parent_id": ModuleTopologyExtractor._parent_id(module_id),
            "depth": 0 if module_id == _ROOT_ID else module_id.count("/") + 1,
            "files": [],
            "file_count": 0,
            "total_file_count": 0,
            "primary_language": "",
            "cohesion": 0.0,
        }

    @staticmethod
    def _owns(module_id: str, key: str) -> bool:
        """True when ``key`` is the module itself or one of its descendants."""
        if key == module_id:
            return True
        if module_id == _ROOT_ID:
            return True
        return key.startswith(module_id + "/")

    @classmethod
    def _subtree_file_count(cls, module_id: str, owner: dict) -> int:
        return sum(len(files) for key, files in owner.items() if cls._owns(module_id, key))

    @classmethod
    def _primary_language(cls, module_id: str, languages: dict) -> str:
        merged: Counter[str] = Counter()
        for key, counts in languages.items():
            if cls._owns(module_id, key):
                merged.update(counts)
        if not merged:
            return ""
        return max(sorted(merged), key=lambda name: merged[name])

    # -------------------------------------------------------------- coupling

    def _annotate_coupling(
        self,
        modules: list[dict],
        meta: dict,
        leaf_of: dict[str, str],
        imports: list[dict],
        calls: list[dict],
    ) -> None:
        chains = self._ancestor_chains(meta)
        scope_depth = self._coupling_depth(modules)
        scope = {
            module_id: chain[min(scope_depth, len(chain) - 1)]
            for module_id, chain in chains.items()
        }

        internal: Counter[str] = Counter()
        external: Counter[str] = Counter()
        depends_on: dict[str, set[str]] = defaultdict(set)
        depended_on_by: dict[str, set[str]] = defaultdict(set)
        seen_pairs: set[tuple[str, str]] = set()
        crossing = 0

        for row in (*imports, *calls):
            source_id = leaf_of.get(row["f1"])
            target_id = leaf_of.get(row["f2"])
            if source_id is None or target_id is None:
                continue

            chain_source = chains[source_id]
            chain_target = chains[target_id]
            target_set = set(chain_target)
            source_set = set(chain_source)
            for module_id in chain_source:
                if module_id in target_set:
                    internal[module_id] += 1
                else:
                    external[module_id] += 1
            for module_id in chain_target:
                if module_id not in source_set:
                    external[module_id] += 1

            if source_id == target_id:
                continue
            bucket_source = scope[source_id]
            bucket_target = scope[target_id]
            if bucket_source == bucket_target:
                continue
            pair = (
                (bucket_source, bucket_target)
                if bucket_source <= bucket_target
                else (bucket_target, bucket_source)
            )
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                crossing += 1
            depends_on[bucket_source].add(bucket_target)
            depended_on_by[bucket_target].add(bucket_source)

        contained = self._contained_pairs(leaf_of, scope, seen_pairs, imports, calls)
        for module in modules:
            module_id = module["id"]
            reflected = internal[module_id] + external[module_id]
            module["cohesion"] = round(internal[module_id] / reflected, 4) if reflected else 0.0
            module["efferent"] = len(depends_on.get(module_id, ()))
            module["afferent"] = len(depended_on_by.get(module_id, ()))

        meta["_dependencies"] = {key: sorted(value) for key, value in depends_on.items()}
        meta["_coupling"] = round(crossing / max(crossing + contained, 1), 4)

    @staticmethod
    def _contained_pairs(
        leaf_of: dict[str, str],
        scope: dict[str, str],
        seen_pairs: set[tuple[str, str]],
        imports: list[dict],
        calls: list[dict],
    ) -> int:
        """Distinct file pairs whose two ends sit inside the same module."""
        contained: set[tuple[str, str]] = set()
        for row in (*imports, *calls):
            source_id = leaf_of.get(row["f1"])
            target_id = leaf_of.get(row["f2"])
            if source_id is None or target_id is None or source_id == target_id:
                continue
            if scope[source_id] != scope[target_id]:
                continue
            pair = (source_id, target_id) if source_id <= target_id else (target_id, source_id)
            if pair not in seen_pairs:
                contained.add(pair)
        return len(contained)

    @staticmethod
    def _coupling_depth(modules: list[dict]) -> int:
        """Shallowest level that splits the repo into at least two modules.

        Coupling is only meaningful between separate modules; measuring it at
        the deepest level would count almost every edge as crossing.
        """
        counts = Counter(module["depth"] for module in modules)
        if not counts:
            return 0
        for depth in sorted(counts):
            if counts[depth] >= 2:
                return depth
        return max(counts)

    @staticmethod
    def _ancestor_chains(meta: dict) -> dict[str, list[str]]:
        chains: dict[str, list[str]] = {}
        for module_id in meta:
            if module_id.startswith("_"):
                continue
            chain: list[str] = []
            current: str | None = module_id
            while current is not None and current not in chain:
                chain.append(current)
                current = meta.get(current, {}).get("parent_id")
            chains[module_id] = list(reversed(chain))
        return chains

    # ------------------------------------------------------------- legacy API

    @staticmethod
    def common_prefix(paths: list[str]) -> str:
        if not paths:
            return ""
        directories = [PurePosixPath(path).parent.parts for path in paths]
        prefix = ModuleTopologyExtractor._common_prefix(list(directories))
        return "/".join(prefix) if prefix else "root"
