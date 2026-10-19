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

import re
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
_SHARED_ID = "shared"
_DOMAIN_DEPTH = 1
_ENTITY_DEPTH = 2

# Bumped whenever the layout or the coupling semantics change. Published reports
# carry it and read-boundary projections compare against it, so an updated
# extractor can supersede a stale topology without republishing a snapshot.
MODULE_TOPOLOGY_VERSION = "module-topology/v3"

# File-name suffixes and directory names that name an architectural layer
# rather than a business entity.  ``impl``/``serviceimpl`` collapse onto the
# service layer; ``param``/``result`` are the DTO family.
_LAYER_SUFFIXES = {
    "controller": "controller",
    "serviceimpl": "service",
    "service": "service",
    "impl": "service",
    "dao": "dao",
    "mapper": "mapper",
    "repository": "repository",
    "example": "example",
    "param": "dto",
    "result": "dto",
    "dto": "dto",
    "vo": "vo",
    "model": "model",
    "entity": "model",
    "config": "config",
    "util": "util",
    "utils": "util",
    "helper": "util",
    "exception": "exception",
    "constant": "constant",
    "converter": "converter",
    "factory": "factory",
    "component": "component",
    "manager": "manager",
    "handler": "handler",
    "listener": "listener",
    "interceptor": "interceptor",
    "filter": "filter",
    "client": "client",
    "request": "request",
    "response": "response",
}

# ``PmsProduct`` -> ``Pms``: the acronym mall and friends prefix a business
# domain with.  A short capitalised run followed by another capital.
_DOMAIN_PREFIX = re.compile(r"^[A-Z][a-z]{1,3}(?=[A-Z])")
# CamelCase / acronym / digit split: ``PmsProductServiceImpl`` -> the four parts.
_NAME_TOKENS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# A repository only gets business-domain modules when file names actually carry
# one.  Below these limits the file-name signal is noise and the path skeleton
# is both safer and, for a repo already organised by business, identical.
_BUSINESS_MIN_FILES = 20
_BUSINESS_MIN_COVERAGE = 0.5
_BUSINESS_TOP_DOMAINS = 5
# A domain needs at least this many distinct entities to be worth a branch.
_BUSINESS_MIN_ENTITIES = 2

# The canonical per-module report shape, shared by analysis and by reads.
_MODULE_FIELDS = (
    "id",
    "name",
    "segment",
    "path",
    "kind",
    "parent_id",
    "depth",
    "file_count",
    "total_file_count",
    "primary_language",
    "layers",
    "layer_conflict",
    "cohesion",
    "afferent",
    "efferent",
)


def serialize_module_topology(topology: ModuleTopology) -> dict[str, Any]:
    """Render a ``ModuleTopology`` as the stored and served report shape."""
    return {
        "version": MODULE_TOPOLOGY_VERSION,
        "module_count": len(topology.modules),
        "coupling_score": topology.coupling_score,
        "modules": [{key: module[key] for key in _MODULE_FIELDS} for module in topology.modules],
        "dependencies": topology.dependency_graph,
    }


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

        Two layouts are possible.  When file names carry a business domain the
        topology is ``domain -> entity`` and the architectural layer becomes a
        per-module attribute.  Otherwise the directory skeleton is used, which
        is already the right answer for a repository organised by business.
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
        assignments = self._business_assignments(source_records)
        scope_depth: int | None
        group_depth: int | None
        if self._accepts_business_layout(source_records, assignments):
            modules, meta, leaf_of = self._business_layout(source_records, assignments)
            # Dependencies between entities, coupling ratio between domains.
            scope_depth, group_depth = _ENTITY_DEPTH, _DOMAIN_DEPTH
        else:
            modules, meta, leaf_of = self._layout(source_records, all_paths, output_depth)
            scope_depth = group_depth = None
        if not modules:
            return ModuleTopology()

        self._annotate_coupling(
            modules,
            meta,
            leaf_of,
            imports,
            calls,
            scope_depth=scope_depth,
            group_depth=group_depth,
        )
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
            "kind": "root" if module_id == _ROOT_ID else "module",
            "parent_id": ModuleTopologyExtractor._parent_id(module_id),
            "depth": 0 if module_id == _ROOT_ID else module_id.count("/") + 1,
            "files": [],
            "file_count": 0,
            "total_file_count": 0,
            "primary_language": "",
            "layers": {},
            "layer_conflict": 0,
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

    # -------------------------------------------------------- business layout

    @staticmethod
    def _name_tokens(stem: str) -> list[str]:
        return _NAME_TOKENS.findall(stem) or [stem]

    @classmethod
    def _split_name(cls, stem: str) -> tuple[str, str]:
        """Split a file name into ``(layer, entity)``.

        The trailing layer word is peeled off longest-match-first, so
        ``PmsProductServiceImpl`` yields ``service`` and ``PmsProduct``.  The
        remainder is the business entity -- the one thing a directory layout
        cannot express, because every layer of an entity lives in its own
        directory.
        """
        tokens = cls._name_tokens(stem)
        for size in (3, 2, 1):
            if len(tokens) <= size:
                continue
            candidate = "".join(tokens[-size:]).lower()
            if candidate in _LAYER_SUFFIXES:
                return _LAYER_SUFFIXES[candidate], "".join(tokens[:-size])
        if len(tokens) == 1 and tokens[0].lower() in _LAYER_SUFFIXES:
            return _LAYER_SUFFIXES[tokens[0].lower()], ""
        return "", "".join(tokens)

    @staticmethod
    def _directory_layer(path: str) -> str:
        for part in reversed(PurePosixPath(path).parent.parts):
            layer = _LAYER_SUFFIXES.get(part.lower())
            if layer is not None:
                return layer
        return ""

    @staticmethod
    def _domain_key(stem: str) -> str:
        match = _DOMAIN_PREFIX.match(stem)
        return match.group(0) if match else ""

    @classmethod
    def _business_assignments(
        cls, source_records: list[tuple[str, str]]
    ) -> list[tuple[str, str, str, str, str]]:
        """Per file: ``(path, language, domain, entity, layer)``.

        A domain needs ``_BUSINESS_MIN_ENTITIES`` distinct entities to earn a
        branch; a one-entity acronym is not a business domain, so its files
        join the shared bucket instead.  Layers come from the file name and
        fall back to the directory.
        """
        rows = []
        for path, language in source_records:
            stem = PurePosixPath(path).stem
            suffix_layer, entity = cls._split_name(stem)
            rows.append(
                (
                    path,
                    language,
                    cls._domain_key(stem),
                    entity,
                    suffix_layer or cls._directory_layer(path),
                )
            )

        entities: dict[str, set[str]] = defaultdict(set)
        for _path, _language, domain, entity, _layer in rows:
            if domain and entity:
                entities[domain].add(entity)
        return [
            (
                path,
                language,
                domain if len(entities.get(domain, ())) >= _BUSINESS_MIN_ENTITIES else "",
                entity,
                layer,
            )
            for path, language, domain, entity, layer in rows
        ]

    @staticmethod
    def _accepts_business_layout(
        source_records: list[tuple[str, str]],
        assignments: list[tuple[str, str, str, str, str]],
    ) -> bool:
        """True when file names discriminate business domains strongly enough.

        Coverage of the largest domains is what separates ``mall`` (76%: Ums /
        Pms / Sms / Oms / Cms) from repositories whose names carry no domain at
        all (``codeos`` 26%, ``mall-admin-web`` 6%).  Falling back is not a
        failure: a repo already organised by business has business
        directories, so the path skeleton returns the same tree.
        """
        if len(source_records) < _BUSINESS_MIN_FILES:
            return False
        counts = Counter(domain for _p, _l, domain, _e, _layer in assignments if domain)
        if not counts:
            return False
        covered = sum(count for _domain, count in counts.most_common(_BUSINESS_TOP_DOMAINS))
        return covered / len(source_records) >= _BUSINESS_MIN_COVERAGE

    @staticmethod
    def _business_module_id(domain: str, entity: str, layer: str, stem: str) -> str:
        if domain and entity:
            return f"{domain}/{entity}"
        return f"{_SHARED_ID}/{entity or layer or stem}"

    @staticmethod
    def _business_kind(module_id: str, owned: bool) -> str:
        if module_id == _ROOT_ID:
            return "root"
        if owned:
            return "entity"
        return "shared" if module_id == _SHARED_ID else "domain"

    @classmethod
    def _common_directory(cls, files: list[str]) -> str:
        if not files:
            return ""
        directories = [PurePosixPath(path).parent.parts for path in files]
        return "/".join(cls._common_prefix(directories))

    def _business_layout(
        self,
        source_records: list[tuple[str, str]],
        assignments: list[tuple[str, str, str, str, str]],
    ) -> tuple[list[dict], dict, dict[str, str]]:
        """Build the ``domain -> entity`` tree, keeping layers as an attribute."""
        owner: dict[str, list[str]] = defaultdict(list)
        languages: dict[str, Counter[str]] = defaultdict(Counter)
        layers: dict[str, Counter[str]] = defaultdict(Counter)
        conflicts: Counter[str] = Counter()
        leaf_of: dict[str, str] = {}

        for path, language, domain, entity, layer in assignments:
            stem = PurePosixPath(path).stem
            module_id = self._business_module_id(domain, entity, layer, stem)
            leaf_of[path] = module_id
            owner[module_id].append(path)
            languages[module_id][language or self._language_of(path)] += 1
            if layer:
                layers[module_id][layer] += 1
                directory = self._directory_layer(path)
                if directory and directory != layer:
                    conflicts[module_id] += 1

        meta: dict[str, Any] = {"_dependencies": {}, "_coupling": 0.0}
        for module_id in list(owner):
            self._ensure_ancestors(meta, module_id)
        meta.setdefault(_ROOT_ID, self._module_meta(_ROOT_ID))

        for module_id, node in meta.items():
            if module_id.startswith("_"):
                continue
            node["files"] = sorted(owner.get(module_id, []))
            node["file_count"] = len(node["files"])
            node["total_file_count"] = self._subtree_file_count(module_id, owner)
            node["primary_language"] = self._primary_language(module_id, languages)
            node["kind"] = self._business_kind(module_id, module_id in owner)
            merged: Counter[str] = Counter()
            for key, counts in layers.items():
                if self._owns(module_id, key):
                    merged.update(counts)
            node["layers"] = dict(sorted(merged.items(), key=lambda item: (-item[1], item[0])))
            node["layer_conflict"] = sum(
                count for key, count in conflicts.items() if self._owns(module_id, key)
            )
            node["path"] = self._common_directory(node["files"]) if module_id in owner else ""

        modules = [node for key, node in meta.items() if not key.startswith("_")]
        return modules, meta, leaf_of

    # -------------------------------------------------------------- coupling

    def _annotate_coupling(
        self,
        modules: list[dict],
        meta: dict,
        leaf_of: dict[str, str],
        imports: list[dict],
        calls: list[dict],
        scope_depth: int | None = None,
        group_depth: int | None = None,
    ) -> None:
        """Annotate cohesion, module dependencies, and the coupling ratio.

        Two scopes are needed once the topology has a real module tree.
        Dependencies are reported between the *leaf* modules -- in a business
        topology that is ``PmsProduct -> PmsCategory``, the useful level.  The
        coupling ratio is measured one level up, between the groups: at the
        leaf level every remaining edge crosses a boundary by construction, so
        the ratio would always read 1.0.
        """
        chains = self._ancestor_chains(meta)
        if scope_depth is None:
            scope_depth = self._coupling_depth(modules)
        if group_depth is None:
            group_depth = self._coupling_depth(modules)
        scope = {
            module_id: chain[min(scope_depth, len(chain) - 1)]
            for module_id, chain in chains.items()
        }
        group = {
            module_id: chain[min(group_depth, len(chain) - 1)]
            for module_id, chain in chains.items()
        }

        internal: Counter[str] = Counter()
        external: Counter[str] = Counter()
        depends_on: dict[str, set[str]] = defaultdict(set)
        depended_on_by: dict[str, set[str]] = defaultdict(set)
        group_depends_on: dict[str, set[str]] = defaultdict(set)
        group_depended_on_by: dict[str, set[str]] = defaultdict(set)
        seen_group: set[tuple[str, str]] = set()
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
            if bucket_source != bucket_target:
                depends_on[bucket_source].add(bucket_target)
                depended_on_by[bucket_target].add(bucket_source)

            group_source = group[source_id]
            group_target = group[target_id]
            if group_source == group_target:
                continue
            group_depends_on[group_source].add(group_target)
            group_depended_on_by[group_target].add(group_source)
            pair = (
                (group_source, group_target)
                if group_source <= group_target
                else (group_target, group_source)
            )
            if pair not in seen_group:
                seen_group.add(pair)
                crossing += 1

        contained = self._contained_pairs(leaf_of, group, seen_group, imports, calls)
        for module in modules:
            module_id = module["id"]
            reflected = internal[module_id] + external[module_id]
            module["cohesion"] = round(internal[module_id] / reflected, 4) if reflected else 0.0
            # A module's relationships are counted at its own level: entities
            # against entities, domains against domains.
            outgoing = group_depends_on if module["depth"] == group_depth else depends_on
            incoming = group_depended_on_by if module["depth"] == group_depth else depended_on_by
            module["efferent"] = len(outgoing.get(module_id, ()))
            module["afferent"] = len(incoming.get(module_id, ()))

        combined: dict[str, set[str]] = defaultdict(set)
        for graph in (group_depends_on, depends_on):
            for key, value in graph.items():
                combined[key].update(value)
        meta["_dependencies"] = {key: sorted(value) for key, value in combined.items()}
        meta["_coupling"] = round(crossing / max(crossing + contained, 1), 4)

    @staticmethod
    def _contained_pairs(
        leaf_of: dict[str, str],
        group: dict[str, str],
        seen_group: set[tuple[str, str]],
        imports: list[dict],
        calls: list[dict],
    ) -> int:
        """Distinct leaf pairs that share a group without being one module."""
        contained: set[tuple[str, str]] = set()
        for row in (*imports, *calls):
            source_id = leaf_of.get(row["f1"])
            target_id = leaf_of.get(row["f2"])
            if source_id is None or target_id is None or source_id == target_id:
                continue
            if group[source_id] != group[target_id]:
                continue
            pair = (source_id, target_id) if source_id <= target_id else (target_id, source_id)
            if pair not in seen_group:
                contained.add(pair)
        return len(contained)

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
