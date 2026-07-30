"""Phase 1: code-to-knowledge extraction from CodeGraph SQLite.

Five knowledge artifacts — purely graph-derived, no LLM required:

  1. API contract  — route → handler → request/response shape
  2. Module topology — community detection on imports + calls
  3. Core entities  — PageRank centrality on the call graph
  4. Test gaps      — production functions with no test coverage
  5. Layer violations — directory-naming convention checks

All queries read from CodeGraph's SQLite via CodeGraphReader.
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx
from networkx.algorithms.community import louvain_communities

from .codegraph_reader import CodeGraphReader, FunctionDef, HTTP_DECORATORS


# ── Layer classification ──────────────────────────────────────────────

# Directory-name → layer mapping.  Matched case-insensitively against
# path segments.  First match wins (most-specific patterns first).
LAYER_PATTERNS = [
    # Presentation / transport
    ("controller", "presentation"),
    ("handler", "presentation"),
    ("view", "presentation"),
    ("route", "presentation"),
    ("router", "presentation"),
    ("middleware", "presentation"),
    ("api", "presentation"),
    ("endpoint", "presentation"),
    ("resource", "presentation"),
    ("serializer", "presentation"),
    # Application / use-case
    ("service", "application"),
    ("usecase", "application"),
    ("use_case", "application"),
    ("interactor", "application"),
    # Domain / business logic
    ("domain", "domain"),
    ("model", "domain"),
    ("entity", "domain"),
    ("valueobject", "domain"),
    ("value_object", "domain"),
    ("aggregate", "domain"),
    # Infrastructure / data access
    ("repository", "infrastructure"),
    ("dao", "infrastructure"),
    ("mapper", "infrastructure"),
    ("persistence", "infrastructure"),
    ("database", "infrastructure"),
    ("db", "infrastructure"),
    ("client", "infrastructure"),
    ("gateway", "infrastructure"),
    ("adapter", "infrastructure"),
    ("config", "infrastructure"),
    ("configuration", "infrastructure"),
    # Test
    ("tests", "test"),
    ("test", "test"),
    ("__tests__", "test"),
    ("spec", "test"),
    ("fixtures", "test"),
    ("e2e", "test"),
    ("integration", "test"),
]

# Allowed call directions: layer_from → may call → layer_to
LAYER_ALLOWED = {
    ("presentation", "application"): True,
    ("presentation", "domain"): True,
    ("presentation", "infrastructure"): False,  # violation
    ("application", "domain"): True,
    ("application", "infrastructure"): True,
    ("domain", "presentation"): False,          # violation
    ("domain", "application"): False,            # violation
    ("domain", "infrastructure"): True,          # via interface/port
    ("infrastructure", "presentation"): False,   # violation
    ("infrastructure", "application"): False,    # violation
    ("infrastructure", "domain"): False,         # violation
}

# File-path patterns for test detection
TEST_PATH_PATTERNS = (
    "/tests/", "/test/", "/__tests__/", "/spec/", "/specs/",
    "/fixtures/", "/e2e/", "/integration/", "/__mocks__/",
    ".test.", ".spec.", "_test.py", "_test.java", "test_", "_test.go",
    "Test.java", "Tests.java", "Test.kt", "Tests.kt",
)


# ── Output data types ──────────────────────────────────────────────────

@dataclass
class ApiEndpoint:
    """A single API endpoint derived from a route + handler."""
    method: str                          # GET / POST / PUT / DELETE
    path: str                            # /api/users/:id
    handler_name: str                    # qualified_name
    file_path: str
    line: int
    params: list[str] = field(default_factory=list)
    return_type: str | None = None
    decorators: list[str] = field(default_factory=list)
    downstream_calls: list[str] = field(default_factory=list)  # qualified_names


@dataclass
class ApiContract:
    """All API endpoints grouped by resource prefix."""
    endpoints: list[ApiEndpoint] = field(default_factory=list)
    resource_groups: dict[str, list[ApiEndpoint]] = field(default_factory=dict)


@dataclass
class ModuleTopology:
    """Community-detected modules and their dependencies."""
    modules: list[dict] = field(default_factory=list)  # [{id, files, primary_language, size}]
    dependency_graph: dict[str, list[str]] = field(default_factory=dict)  # module_id → [dep_module_ids]
    coupling_score: float = 0.0  # avg inter-module edges / total edges


@dataclass
class CoreEntity:
    """A domain-significant class/function ranked by graph centrality."""
    node_id: str
    name: str
    qualified_name: str
    file_path: str
    kind: str
    pagerank: float
    in_degree: int       # how many distinct callers
    out_degree: int      # how many distinct callees
    layer: str = ""


@dataclass
class TestGap:
    """A production function with zero test coverage."""
    node_id: str
    name: str
    qualified_name: str
    file_path: str
    kind: str
    line: int
    is_exported: bool = False


@dataclass
class LayerViolation:
    """A call that crosses a forbidden layer boundary."""
    source_name: str
    source_file: str
    source_layer: str
    target_name: str
    target_file: str
    target_layer: str
    call_line: int | None = None


# ── Knowledge extractor ────────────────────────────────────────────────

class KnowledgeExtractor:
    """Extracts business knowledge from CodeGraph's knowledge graph."""

    def __init__(self, reader: CodeGraphReader):
        self.reader = reader
        self._function_cache: dict[str, FunctionDef] | None = None
        self._call_graph: nx.DiGraph | None = None

    # ── 1. API contract ────────────────────────────────────────────────

    def extract_api_contract(self) -> ApiContract:
        """Generate API contract from route nodes + HTTP-decorated functions."""
        endpoints: list[ApiEndpoint] = []

        # Source 1: explicit route nodes (framework-resolved by CodeGraph)
        route_nodes = self._query(
            "SELECT name, file_path, start_line FROM nodes WHERE kind = 'route'"
        )
        # route nodes typically have names like "GET /api/users" already
        for rn in route_nodes:
            method, _, path = rn["name"].partition(" ")
            if not method or not path:
                continue
            endpoints.append(ApiEndpoint(
                method=method.upper(),
                path=path,
                handler_name="",
                file_path=rn["file_path"],
                line=rn["start_line"],
            ))

        # Source 2: functions with HTTP decorators
        http_funcs = self._query("""
            SELECT id, name, qualified_name, file_path, start_line,
                   signature, decorators
            FROM nodes
            WHERE kind IN ('function', 'method') AND decorators IS NOT NULL
        """)
        for f in http_funcs:
            decos_raw = f["decorators"]
            if not decos_raw:
                continue
            try:
                decos = json.loads(decos_raw) if isinstance(decos_raw, str) else decos_raw
            except (json.JSONDecodeError, TypeError):
                continue

            for deco in decos:
                deco_lower = deco.lstrip("@").lower()
                deco_name = deco_lower.split(".")[-1]
                if deco_name not in HTTP_DECORATORS:
                    continue

                method = HTTP_DECORATORS[deco_name]
                # Infer path from function name / decorator text
                path = self._infer_path(deco, f["name"], f["file_path"])

                # Parse params from signature
                params = self._parse_params(f.get("signature", ""))

                endpoints.append(ApiEndpoint(
                    method=method or "ANY",
                    path=path,
                    handler_name=f["qualified_name"],
                    file_path=f["file_path"],
                    line=f["start_line"],
                    params=params,
                    return_type=self._parse_return_type(f.get("signature", "")),
                    decorators=[deco],
                ))

        # Group by resource prefix
        groups: dict[str, list[ApiEndpoint]] = defaultdict(list)
        for ep in endpoints:
            prefix = self._resource_prefix(ep.path)
            groups[prefix].append(ep)

        return ApiContract(
            endpoints=endpoints,
            resource_groups=dict(sorted(groups.items())),
        )

    @staticmethod
    def _infer_path(deco_text: str, func_name: str, file_path: str) -> str:
        """Infer URL path from decorator text or naming convention."""
        # Try to extract path from decorator: @app.get("/users") → "/users"
        import re
        m = re.search(r'''['"](/[^'"]*)['"]''', deco_text)
        if m:
            return m.group(1)

        # Fallback: derive from function name
        # get_user_by_id → /user/:id, post_order → /order
        name = func_name
        for prefix in ("get_", "post_", "put_", "delete_", "patch_", "head_"):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        parts = name.split("_")
        path_parts = []
        for p in parts:
            if p in ("by", "with", "from", "for"):
                continue
            if p == "id" or p.endswith("_id"):
                path_parts.append(":id")
            else:
                path_parts.append(p)
        return "/" + "/".join(path_parts) if path_parts else "/"

    @staticmethod
    def _resource_prefix(path: str) -> str:
        """Extract resource prefix from path: /api/users/:id → users"""
        clean = path.strip("/")
        parts = clean.split("/")
        # Skip common prefixes
        skip = {"api", "v1", "v2", "v3", "v4"}
        for p in parts:
            if p.lower() not in skip and not p.startswith(":"):
                return p
        return parts[-1] if parts else "root"

    @staticmethod
    def _parse_params(signature: str) -> list[str]:
        """Extract parameter names from a function signature."""
        if not signature or "(" not in signature:
            return []
        try:
            params_str = signature.split("(", 1)[1].rsplit(")", 1)[0]
        except IndexError:
            return []
        params = []
        for p in params_str.split(","):
            p = p.strip()
            if not p or p == "self":
                continue
            # Split on first colon (type annotation) or first space
            if ":" in p:
                name = p.split(":")[0].strip()
            else:
                name = p.split()[0].strip() if p.split() else p
            params.append(name)
        return params

    @staticmethod
    def _parse_return_type(signature: str) -> str | None:
        """Extract return type from signature: (...) -> Type"""
        if "->" in signature:
            return signature.split("->")[-1].strip().rstrip(":")
        return None

    # ── 2. Module topology ─────────────────────────────────────────────

    def extract_module_topology(self, resolution: float = 0.8) -> ModuleTopology:
        """Detect modules via Louvain community detection on imports + calls.

        Args:
            resolution: Louvain resolution parameter (>1 = more modules).
        """
        G = self._build_module_graph()

        if G.number_of_edges() == 0:
            return ModuleTopology()

        # Louvain community detection
        communities = louvain_communities(G, resolution=resolution, seed=42)

        # Build module descriptions
        modules = []
        file_to_module: dict[str, int] = {}
        for idx, comm in enumerate(communities):
            module_files = sorted(comm)
            # Primary language by majority vote
            lang_counts: dict[str, int] = defaultdict(int)
            for fp in module_files:
                file_to_module[fp] = idx
                ext = Path(fp).suffix.lower()
                lang_counts[ext] = lang_counts.get(ext, 0) + 1
            primary_lang = max(lang_counts, key=lang_counts.get) if lang_counts else ""

            modules.append({
                "id": f"mod-{idx + 1}",
                "files": module_files,
                "file_count": len(module_files),
                "primary_language": primary_lang,
                # Representative name: common directory prefix
                "name": self._common_prefix(module_files),
            })

        # Inter-module dependency graph
        deps: dict[str, set[str]] = defaultdict(set)
        inter_edges = 0
        total_edges = 0
        for u, v in G.edges():
            total_edges += 1
            mu = file_to_module.get(u)
            mv = file_to_module.get(v)
            if mu is not None and mv is not None:
                if mu != mv:
                    deps[f"mod-{mu + 1}"].add(f"mod-{mv + 1}")
                    inter_edges += 1

        coupling = inter_edges / max(total_edges, 1)

        return ModuleTopology(
            modules=sorted(modules, key=lambda m: -m["file_count"]),
            dependency_graph={k: sorted(v) for k, v in deps.items()},
            coupling_score=round(coupling, 4),
        )

    def _build_module_graph(self) -> nx.Graph:
        """Build an undirected weighted graph: files are nodes, imports/calls are edges."""
        G = nx.Graph()

        # Edge weight = import + call count between two files
        import_rows = self._query("""
            SELECT n1.file_path AS f1, n2.file_path AS f2
            FROM edges e
            JOIN nodes n1 ON n1.id = e.source
            JOIN nodes n2 ON n2.id = e.target
            WHERE e.kind = 'imports'
        """)
        for r in import_rows:
            f1, f2 = r["f1"], r["f2"]
            if f1 == f2:
                continue
            if G.has_edge(f1, f2):
                G[f1][f2]["weight"] += 2  # imports are strong structural coupling
            else:
                G.add_edge(f1, f2, weight=2)

        call_rows = self._query("""
            SELECT n1.file_path AS f1, n2.file_path AS f2
            FROM edges e
            JOIN nodes n1 ON n1.id = e.source
            JOIN nodes n2 ON n2.id = e.target
            WHERE e.kind = 'calls' AND n1.file_path != n2.file_path
        """)
        for r in call_rows:
            f1, f2 = r["f1"], r["f2"]
            if G.has_edge(f1, f2):
                G[f1][f2]["weight"] += 1
            else:
                G.add_edge(f1, f2, weight=1)

        return G

    @staticmethod
    def _common_prefix(paths: list[str]) -> str:
        """Longest common directory prefix for a set of paths."""
        if not paths:
            return ""
        dirs = [Path(p).parent.parts for p in paths]
        min_len = min(len(d) for d in dirs)
        prefix = []
        for i in range(min_len):
            segment = dirs[0][i]
            if all(d[i] == segment for d in dirs):
                prefix.append(segment)
            else:
                break
        return "/".join(prefix) if prefix else "root"

    # ── 3. Core entities ───────────────────────────────────────────────

    def extract_core_entities(self, top_n: int = 30) -> list[CoreEntity]:
        """Identify core entities via PageRank centrality on the call graph."""
        G = self._get_call_graph()
        if G.number_of_nodes() == 0:
            return []

        # Pure-Python PageRank (avoids scipy dependency)
        pr = self._pagerank_python(G, alpha=0.85, max_iter=100)
        ranked = sorted(pr.items(), key=lambda x: -x[1])[:top_n]

        entities = []
        for node_id, score in ranked:
            func = self.reader.get_function_by_id(node_id)
            if func is None:
                continue

            entities.append(CoreEntity(
                node_id=node_id,
                name=func.name,
                qualified_name=func.qualified_name,
                file_path=func.file_path,
                kind=func.kind,
                pagerank=round(score, 6),
                in_degree=G.in_degree(node_id),
                out_degree=G.out_degree(node_id),
                layer=self._classify_file_layer(func.file_path),
            ))

        return entities

    def _get_call_graph(self) -> nx.DiGraph:
        """Build a directed call graph: node_id → node_id (lazy-cached)."""
        if self._call_graph is not None:
            return self._call_graph

        G = nx.DiGraph()
        rows = self._query("""
            SELECT source, target FROM edges
            WHERE kind = 'calls'
        """)
        for r in rows:
            G.add_edge(r["source"], r["target"])

        self._call_graph = G
        return G

    # ── 4. Test gaps ───────────────────────────────────────────────────

    def extract_test_gaps(self) -> list[TestGap]:
        """Find production functions that have zero test coverage."""
        all_funcs = self.reader.get_all_functions()

        # Partition into test vs production
        test_funcs: list[FunctionDef] = []
        prod_funcs: list[FunctionDef] = []
        for f in all_funcs:
            if self._is_test_function(f):
                test_funcs.append(f)
            else:
                prod_funcs.append(f)

        if not prod_funcs:
            return []

        # All node_ids that are called (directly) from any test function
        covered: set[str] = set()
        for tf in test_funcs:
            callees = self.reader.get_callees(tf.node_id)
            for c in callees:
                covered.add(c.callee_node_id)

        # Production functions NOT in covered set = gaps
        gaps = []
        for f in prod_funcs:
            if f.node_id not in covered:
                gaps.append(TestGap(
                    node_id=f.node_id,
                    name=f.name,
                    qualified_name=f.qualified_name,
                    file_path=f.file_path,
                    kind=f.kind,
                    line=f.start_line,
                    is_exported=f.is_exported,
                ))

        return gaps

    def extract_test_coverage_stats(self) -> dict:
        """Summary stats for test coverage."""
        all_funcs = self.reader.get_all_functions()
        test_funcs = [f for f in all_funcs if self._is_test_function(f)]
        prod_funcs = [f for f in all_funcs if not self._is_test_function(f)]

        covered: set[str] = set()
        for tf in test_funcs:
            for c in self.reader.get_callees(tf.node_id):
                covered.add(c.callee_node_id)

        prod_covered = sum(1 for f in prod_funcs if f.node_id in covered)
        prod_total = len(prod_funcs)

        return {
            "test_functions": len(test_funcs),
            "production_functions": prod_total,
            "covered_functions": prod_covered,
            "coverage_pct": round(100 * prod_covered / max(prod_total, 1), 1),
            "gap_count": prod_total - prod_covered,
        }

    @staticmethod
    def _is_test_function(func: FunctionDef) -> bool:
        """Determine if a function/method is a test."""
        if func.is_test:
            return True
        fp = func.file_path.lower()
        return any(p in fp for p in TEST_PATH_PATTERNS)

    # ── 5. Layer violations ────────────────────────────────────────────

    def extract_layer_violations(self) -> list[LayerViolation]:
        """Detect calls that cross forbidden layer boundaries."""
        violations: list[LayerViolation] = []

        # Classify all files into layers
        file_layers: dict[str, str] = {}
        all_files = self.reader.get_all_files()
        for fp in all_files:
            file_layers[fp] = self._classify_file_layer(fp)

        # Get calls that cross file boundaries
        rows = self._query("""
            SELECT n1.file_path AS source_file, n1.name AS source_name,
                   n2.file_path AS target_file, n2.name AS target_name,
                   e.line AS call_line
            FROM edges e
            JOIN nodes n1 ON n1.id = e.source
            JOIN nodes n2 ON n2.id = e.target
            WHERE e.kind = 'calls'
              AND n1.file_path != n2.file_path
        """)

        # Batch check: group by source_file → target_file to compute layer pairs
        seen_pairs: set[tuple[str, str]] = set()
        for r in rows:
            sf, tf = r["source_file"], r["target_file"]
            pair_key = (sf, tf)
            if pair_key in seen_pairs:
                continue

            source_layer = file_layers.get(sf, "")
            target_layer = file_layers.get(tf, "")

            # Skip self-layer calls and unknown layers
            if not source_layer or not target_layer:
                continue
            if source_layer == target_layer:
                continue
            if source_layer == "test" or target_layer == "test":
                continue  # tests can call anything

            allowed = LAYER_ALLOWED.get((source_layer, target_layer))
            if allowed is False:
                seen_pairs.add(pair_key)
                violations.append(LayerViolation(
                    source_name=r["source_name"],
                    source_file=sf,
                    source_layer=source_layer,
                    target_name=r["target_name"],
                    target_file=tf,
                    target_layer=target_layer,
                    call_line=r.get("call_line"),
                ))

        return violations

    @staticmethod
    def _classify_file_layer(file_path: str) -> str:
        """Classify a file into an architectural layer by directory naming."""
        fp = file_path.lower()
        for pattern, layer in LAYER_PATTERNS:
            # Check if any path segment matches the pattern
            segments = fp.replace("\\", "/").split("/")
            for seg in segments:
                if seg == pattern or seg.startswith(f"{pattern}_") or seg.endswith(f"_{pattern}"):
                    return layer
        return ""

    # ── Combined report ─────────────────────────────────────────────────

    def extract_all(self) -> dict:
        """Run all five extractors, return one report dict."""
        api = self.extract_api_contract()
        modules = self.extract_module_topology()
        entities = self.extract_core_entities(30)
        gaps = self.extract_test_gaps()
        coverage = self.extract_test_coverage_stats()
        violations = self.extract_layer_violations()

        return {
            "api_contract": {
                "endpoint_count": len(api.endpoints),
                "endpoints": [
                    {
                        "method": ep.method,
                        "path": ep.path,
                        "handler": ep.handler_name,
                        "file": ep.file_path,
                        "line": ep.line,
                        "params": ep.params,
                        "return_type": ep.return_type,
                    }
                    for ep in api.endpoints[:100]  # cap for display
                ],
                "resource_groups": {
                    k: [{"method": ep.method, "path": ep.path, "handler": ep.handler_name}
                        for ep in v[:10]]
                    for k, v in api.resource_groups.items()
                },
            },
            "module_topology": {
                "module_count": len(modules.modules),
                "coupling_score": modules.coupling_score,
                "modules": [
                    {
                        "id": m["id"],
                        "name": m["name"],
                        "file_count": m["file_count"],
                        "primary_language": m["primary_language"],
                    }
                    for m in modules.modules
                ],
                "dependencies": modules.dependency_graph,
            },
            "core_entities": [
                {
                    "name": e.name,
                    "qualified_name": e.qualified_name,
                    "file_path": e.file_path,
                    "kind": e.kind,
                    "pagerank": e.pagerank,
                    "in_degree": e.in_degree,
                    "out_degree": e.out_degree,
                    "layer": e.layer,
                }
                for e in entities
            ],
            "test_coverage": {
                **coverage,
                "top_gaps": [
                    {
                        "name": g.name,
                        "qualified_name": g.qualified_name,
                        "file_path": g.file_path,
                        "kind": g.kind,
                        "line": g.line,
                        "is_exported": g.is_exported,
                    }
                    for g in gaps[:50]
                ],
            },
            "layer_violations": {
                "violation_count": len(violations),
                "violations": [
                    {
                        "source": v.source_name,
                        "source_file": v.source_file,
                        "source_layer": v.source_layer,
                        "target": v.target_name,
                        "target_file": v.target_file,
                        "target_layer": v.target_layer,
                    }
                    for v in violations[:50]
                ],
            },
        }

    @staticmethod
    def _pagerank_python(
        G: nx.DiGraph, alpha: float = 0.85, max_iter: int = 100, tol: float = 1e-6
    ) -> dict[str, float]:
        """Pure-Python PageRank — no scipy dependency."""
        nodes = list(G.nodes())
        n = len(nodes)
        if n == 0:
            return {}

        # Build adjacency matrix as sparse dict
        out_degree = {u: max(G.out_degree(u), 1) for u in nodes}
        personalization = {u: 1.0 / n for u in nodes}

        pr = dict(personalization)
        for _ in range(max_iter):
            prev = dict(pr)
            dangling_sum = alpha * sum(
                prev[u] for u in nodes if G.out_degree(u) == 0
            ) / n
            for u in nodes:
                pr[u] = dangling_sum + (1.0 - alpha) / n
                for v in G.predecessors(u):
                    pr[u] += alpha * prev[v] / out_degree[v]

            # Check convergence
            err = sum(abs(pr[u] - prev[u]) for u in nodes)
            if err < tol * n:
                break

        return pr

    # ── Internal helpers ────────────────────────────────────────────────

    def _query(self, sql: str, params: list | None = None) -> list[dict]:
        """Run a read-only query against CodeGraph's SQLite."""
        cur = self.reader.conn.execute(sql, params or [])
        rows = cur.fetchall()
        return [dict(r) for r in rows]
