"""Code-to-knowledge extraction from CodeGraph SQLite.

Phase 1 — purely graph-derived, no LLM required:
  1. API contract     — route → handler → request/response shape
  2. Module topology  — community detection on imports + calls
  3. Core entities    — PageRank centrality on the call graph
  4. Test gaps        — production functions with no test coverage
  5. Layer violations — directory-naming convention checks

Phase 2 — graph + simple rules, no LLM required:
  6. Config consumption — trace which functions consume which config keys
  7. External deps      — HTTP/DB/cache/message-queue dependency inventory
  8. Authorization model — role-permission matrix from decorators/middleware
  9. Heat map           — call-frequency hot/warm/cold categorization

All queries read from CodeGraph's SQLite via CodeGraphReader.
"""

import json
from collections import defaultdict
from pathlib import Path

import networkx as nx
from networkx.algorithms.community import louvain_communities

from .codegraph_reader import CodeGraphReader, FunctionDef, HTTP_DECORATORS
from .domain.knowledge import (
    ApiContract,
    ApiEndpoint,
    CoreEntity,
    LayerViolation,
    ModuleTopology,
    TestGap,
)
from .infrastructure.source_filesystem import FileSystemSourceProvider
from .ports import SourceProvider


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


# ── Knowledge extractor ────────────────────────────────────────────────

class KnowledgeExtractor:
    """Extracts business knowledge from CodeGraph's knowledge graph."""

    def __init__(self, reader: CodeGraphReader, source_provider: SourceProvider | None = None):
        self.reader = reader
        repo_root = Path(reader.db_path).parent.parent
        self.source_provider = source_provider or FileSystemSourceProvider(repo_root)
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

    def extract_all(self, include_llm: bool = False) -> dict:
        """Run all extractors, return one report dict.

        Args:
            include_llm: If True, also run Phase 3 LLM-powered extractors.
                         Requires OPENAI_API_KEY or ANTHROPIC_API_KEY set.
        """
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
            # ── Phase 2 additions ──────────────────────────────────────
            "config_consumption": self._serialize_config_consumption(
                self.extract_config_consumption()
            ),
            "external_dependencies": self._serialize_external_deps(
                self.extract_external_dependencies()
            ),
            "authorization_model": self._serialize_auth_model(
                self.extract_authorization_model()
            ),
            "heat_map": self._serialize_heat_map(
                self.extract_heat_map()
            ),
            # ── Phase 3 (LLM) — only when enabled ──────────────────────
            "business_descriptions": (
                self.extract_business_descriptions(limit=15)
                if include_llm else {"note": "Set --llm flag to enable LLM analysis"}
            ),
            "business_rules": (
                self.extract_business_rules_llm(limit=10)
                if include_llm else {"note": "Set --llm flag to enable LLM analysis"}
            ),
            "error_catalog": (
                self.extract_error_catalog(limit=15)
                if include_llm else {"note": "Set --llm flag to enable LLM analysis"}
            ),
            "state_machines": (
                self.extract_state_machines()
                if include_llm else {"note": "Set --llm flag to enable LLM analysis"}
            ),
        }

    # ═══════════════════════════════════════════════════════════════════
    # Phase 2 — graph + simple rules
    # ═══════════════════════════════════════════════════════════════════

    # ── 6. Config consumption ──────────────────────────────────────────

    # Known config-file extensions (from CodeGraph files table)
    CONFIG_EXTS = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg",
                   ".conf", ".properties", ".env", ".xml"}

    # Patterns for detecting config-key usage in code
    CONFIG_ACCESS_PATTERNS = [
        # Python: os.getenv("KEY"), os.environ["KEY"], config["key"], settings.KEY
        "os.getenv", "os.environ", "config[", "settings.",
        # JS/TS: process.env.KEY, config.KEY, Config.get("KEY")
        "process.env", "config.", "Config.get",
        # Java: @Value("${...}"), System.getenv, Properties.getProperty
        "@Value", "System.getenv", "getProperty",
        # Go: os.Getenv, viper.Get, config.Get
        "os.Getenv", "viper.Get", "config.Get",
        # General: getenv, get_config, get_env, env.
        "getenv", "get_config", "get_env",
    ]

    def extract_config_consumption(self) -> list[dict]:
        """Identify config files and trace which functions consume which keys.

        Strategy:
          1. Find config files in the repo (from CodeGraph's files table)
          2. Read their keys (from disk — lightweight regex extraction)
          3. Find functions/constants that reference those keys
          4. Trace the call chain from config consumers to business logic
        """
        results: list[dict] = []

        # Step 1: Find config files
        config_files = self._query(
            "SELECT path, language FROM files WHERE language = 'yaml' OR language = 'properties'"
        )
        # Also detect by extension for files CodeGraph classified as 'unknown'
        all_files = self._query("SELECT path, language FROM files")
        config_paths: list[str] = [r["path"] for r in config_files]
        for f in all_files:
            ext = Path(f["path"]).suffix.lower()
            if ext in self.CONFIG_EXTS and f["path"] not in config_paths:
                config_paths.append(f["path"])

        # Step 2: Extract keys from config files (lightweight, regex-based)
        config_keys: dict[str, list[str]] = {}  # file_path → [key1, key2, ...]
        for cf in config_paths:
            keys = self._extract_config_keys(cf)
            if keys:
                config_keys[cf] = keys

        # Step 3: Find functions/constants whose names or signatures match config keys
        all_key_names: set[str] = set()
        for keys in config_keys.values():
            all_key_names.update(k.lower() for k in keys)

        # Find variable and constant nodes that look like config references
        config_ref_nodes = self._query("""
            SELECT id, name, qualified_name, file_path, kind, start_line, decorators
            FROM nodes
            WHERE kind IN ('variable', 'constant', 'function', 'method')
        """)

        # Match: function name contains a config key, or function references env vars
        consumer_map: dict[str, list[str]] = defaultdict(list)  # config_key → [node_ids]
        for node in config_ref_nodes:
            name_lower = node["name"].lower()
            # Direct name match
            for key in all_key_names:
                if key in name_lower or name_lower in key:
                    consumer_map[key].append(node["id"])
            # Check decorators for @Value / config annotations
            decos = node.get("decorators") or ""
            if decos:
                import re as _re
                # Spring @Value("${config.key}")
                for m in _re.finditer(r'\$\{([^}]+)\}', str(decos)):
                    consumer_map[m.group(1).lower()].append(node["id"])

        # Step 4: Build consumption report per config file
        for cf, keys in sorted(config_keys.items()):
            file_consumers: list[dict] = []
            for key in keys:
                consumers = consumer_map.get(key.lower(), [])
                if consumers:
                    # Get consumer details
                    for cid in consumers[:5]:  # cap per key
                        func = self.reader.get_function_by_id(cid)
                        if func:
                            file_consumers.append({
                                "config_key": key,
                                "consumer_name": func.qualified_name,
                                "consumer_file": func.file_path,
                                "consumer_line": func.start_line,
                            })

            if file_consumers:
                results.append({
                    "config_file": cf,
                    "key_count": len(keys),
                    "consumed_keys": len({c["config_key"] for c in file_consumers}),
                    "consumers": file_consumers[:50],
                })

        return results

    def _extract_config_keys(self, file_path: str) -> list[str]:
        """Lightweight config-key extraction without parsing the full file."""
        content = self.source_provider.read_text(file_path)
        if content is None:
            return []

        ext = Path(file_path).suffix.lower()
        import re as _re

        keys: list[str] = []
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//") or line.startswith(";"):
                continue

            if ext in (".yaml", ".yml"):
                m = _re.match(r'^(\s*)([\w_-]+)\s*:', line)
                if m:
                    keys.append(m.group(2))

            elif ext == ".json":
                m = _re.match(r'^\s*"([^"]+)"\s*:', line)
                if m:
                    keys.append(m.group(1))

            elif ext in (".env",):
                m = _re.match(r'^([A-Z_][A-Z0-9_]*)\s*=', line)
                if m:
                    keys.append(m.group(1))

            elif ext in (".toml", ".ini", ".cfg", ".conf"):
                m = _re.match(r'^([\w_-]+)\s*[=:]', line)
                if m:
                    keys.append(m.group(1))
                m = _re.match(r'^\[([^\]]+)\]', line)
                if m:
                    keys.append(m.group(1))

            elif ext in (".properties",):
                m = _re.match(r'^([\w.\\-]+)\s*[=:]', line)
                if m:
                    keys.append(m.group(1).strip())

        return sorted(set(keys))

    # ── 7. External dependencies ────────────────────────────────────────

    # Third-party package prefixes — not from the project itself
    KNOWN_SERVICE_PATTERNS = {
        # HTTP clients
        "requests.": ("http-client", "Python requests"),
        "httpx.": ("http-client", "Python httpx"),
        "fetch(": ("http-client", "JS fetch"),
        "axios.": ("http-client", "JS axios"),
        "HttpClient": ("http-client", "Java HttpClient"),
        "RestTemplate": ("http-client", "Spring RestTemplate"),
        "WebClient": ("http-client", "Spring WebClient"),
        "OkHttp": ("http-client", "Java OkHttp"),
        "got(": ("http-client", "JS got"),
        "node-fetch": ("http-client", "JS node-fetch"),
        # Database
        "sqlalchemy": ("database", "SQLAlchemy"),
        "psycopg": ("database", "PostgreSQL driver"),
        "pymongo": ("database", "MongoDB driver"),
        "redis.": ("database", "Redis client"),
        "aioredis": ("database", "Redis async"),
        "jdbc:": ("database", "JDBC"),
        "jpa.": ("database", "JPA"),
        "hibernate": ("database", "Hibernate"),
        "mongoose": ("database", "Mongoose ODM"),
        "prisma": ("database", "Prisma ORM"),
        "typeorm": ("database", "TypeORM"),
        "drizzle": ("database", "Drizzle ORM"),
        "knex": ("database", "Knex query builder"),
        "sequelize": ("database", "Sequelize ORM"),
        "gorm.": ("database", "GORM"),
        "sqlx.": ("database", "SQLx"),
        "diesel": ("database", "Diesel ORM"),
        # Message queue
        "kafka": ("message-queue", "Apache Kafka"),
        "rabbitmq": ("message-queue", "RabbitMQ"),
        "amqp.": ("message-queue", "AMQP"),
        "pulsar": ("message-queue", "Apache Pulsar"),
        "nats.": ("message-queue", "NATS"),
        "celery": ("message-queue", "Celery task queue"),
        "bull.": ("message-queue", "Bull queue"),
        "sqs.": ("message-queue", "AWS SQS"),
        "pubsub": ("message-queue", "Google PubSub"),
        # Cache
        "memcached": ("cache", "Memcached"),
        "cache.": ("cache", "Cache library"),
        "lru_cache": ("cache", "LRU Cache"),
        # Cloud / storage
        "boto3": ("cloud", "AWS SDK (boto3)"),
        "aws-sdk": ("cloud", "AWS SDK"),
        "google-cloud": ("cloud", "Google Cloud SDK"),
        "azure-": ("cloud", "Azure SDK"),
        "minio.": ("cloud", "MinIO object storage"),
        # Search
        "elasticsearch": ("search", "Elasticsearch"),
        "opensearch": ("search", "OpenSearch"),
        "meilisearch": ("search", "Meilisearch"),
        "algolia": ("search", "Algolia"),
        # Observability
        "prometheus": ("observability", "Prometheus"),
        "opentelemetry": ("observability", "OpenTelemetry"),
        "sentry": ("observability", "Sentry error tracking"),
        "datadog": ("observability", "Datadog"),
        "newrelic": ("observability", "New Relic"),
        "logging.": ("observability", "Logging library"),
        # Auth
        "oauth": ("auth", "OAuth"),
        "jwt.": ("auth", "JWT"),
        "passport": ("auth", "Passport.js"),
        "bcrypt": ("auth", "bcrypt"),
    }

    def extract_external_dependencies(self) -> list[dict]:
        """Inventory external service dependencies from import + call patterns.

        Identifies which services/libraries the code depends on, grouped by
        category (database, http-client, message-queue, cache, cloud, etc.).
        """
        # Collect all import statements and function calls that go to externals
        categories: dict[str, dict[str, list[dict]]] = defaultdict(
            lambda: defaultdict(list)
        )

        # From imports
        import_rows = self._query("""
            SELECT n1.name AS import_name, n1.file_path AS importer_file,
                   n1.start_line, n1.signature
            FROM nodes n1
            WHERE n1.kind = 'import'
        """)
        for r in import_rows:
            name = (r.get("signature") or r["import_name"]).lower()
            cat, label = self._classify_import(name)
            if cat:
                categories[cat][label].append({
                    "file": r["importer_file"],
                    "line": r["start_line"],
                    "source": "import",
                })

        # From decorators (framework annotations like @EnableJpaRepositories)
        deco_rows = self._query("""
            SELECT DISTINCT decorators, file_path, start_line
            FROM nodes
            WHERE decorators IS NOT NULL
        """)
        for r in deco_rows:
            try:
                decos = json.loads(r["decorators"]) if isinstance(r["decorators"], str) else r["decorators"]
            except (json.JSONDecodeError, TypeError):
                continue
            for deco in decos:
                cat, label = self._classify_import(deco.lower())
                if cat:
                    categories[cat][label].append({
                        "file": r["file_path"],
                        "line": r["start_line"],
                        "source": "decorator",
                    })

        # Build result
        result: list[dict] = []
        for cat in sorted(categories):
            deps = categories[cat]
            items: list[dict] = []
            for label, uses in deps.items():
                # Unique files
                files = sorted(set(u["file"] for u in uses))
                items.append({
                    "label": label,
                    "file_count": len(files),
                    "files": files[:10],
                })
            result.append({
                "category": cat,
                "dependency_count": len(items),
                "dependencies": items,
            })

        return result

    @staticmethod
    def _classify_import(name: str) -> tuple[str | None, str | None]:
        """Classify an import/call name into (category, label)."""
        name_lower = name.lower()
        for pattern, (cat, label) in KnowledgeExtractor.KNOWN_SERVICE_PATTERNS.items():
            if pattern.lower() in name_lower:
                return cat, label
        return None, None

    # ── 8. Authorization model ──────────────────────────────────────────

    # Permission/role decorator patterns across frameworks
    AUTH_DECORATOR_PATTERNS = {
        # Python: Flask/Django/FastAPI
        "login_required": "authenticated",
        "permission_required": None,      # extract argument
        "has_permission": None,
        "has_role": None,
        "requires_auth": "authenticated",
        "require_auth": "authenticated",
        "authenticated": "authenticated",
        # Java: Spring Security
        "preauthorize": None,
        "postauthorize": None,
        "secured": None,
        "rolesallowed": None,
        "permitall": "public",
        "denyall": "denied",
        # JS/TS: NestJS/Passport
        "useguards": None,
        "roles": None,
        "requireauth": "authenticated",
        "public": "public",
        "authenticated": "authenticated",
        # Middleware patterns (function names)
        "auth_middleware": "authenticated",
        "authmiddleware": "authenticated",
        "requirepermission": None,
        "authorize": None,
    }

    def extract_authorization_model(self) -> list[dict]:
        """Extract role-permission matrix from decorators and middleware.

        Returns list of {endpoint, roles, permissions, auth_mechanism}.
        """
        results: list[dict] = []

        # Find all functions/methods with auth-related decorators
        auth_rows = self._query("""
            SELECT id, name, qualified_name, file_path, start_line, decorators, kind
            FROM nodes
            WHERE kind IN ('function', 'method') AND decorators IS NOT NULL
        """)

        for r in auth_rows:
            try:
                decos = json.loads(r["decorators"]) if isinstance(r["decorators"], str) else r["decorators"]
            except (json.JSONDecodeError, TypeError):
                continue

            roles: list[str] = []
            permissions: list[str] = []
            auth_level = "unknown"

            for deco in decos:
                deco_lower = deco.lstrip("@").lower()
                # Extract decorator base name and arguments
                import re as _re
                m = _re.match(r'([\w.]+)(?:\(([^)]*)\))?', deco_lower)
                if not m:
                    continue
                deco_name = m.group(1).split(".")[-1]
                deco_args = m.group(2) or ""

                if deco_name in self.AUTH_DECORATOR_PATTERNS:
                    level = self.AUTH_DECORATOR_PATTERNS[deco_name]
                    if level and level != "unknown":
                        auth_level = level

                    # Extract role/permission strings from decorator arguments
                    if deco_args:
                        args = [a.strip().strip("\"'") for a in deco_args.split(",")]
                        for a in args:
                            a_clean = a.strip("[]()\"' ")
                            if a_clean and a_clean not in ("", " "):
                                if deco_name in ("has_role", "rolesallowed", "roles"):
                                    roles.append(a_clean)
                                else:
                                    permissions.append(a_clean)

            if auth_level != "unknown" or roles or permissions:
                results.append({
                    "function": r["qualified_name"],
                    "file": r["file_path"],
                    "line": r["start_line"],
                    "auth_level": auth_level,
                    "roles": sorted(set(roles)),
                    "permissions": sorted(set(permissions)),
                })

        # Also detect middleware functions by name
        mid_rows = self._query("""
            SELECT id, name, qualified_name, file_path, start_line, kind
            FROM nodes
            WHERE kind IN ('function', 'method')
              AND (name LIKE '%auth%middleware%'
                OR name LIKE '%auth%guard%'
                OR name LIKE '%permission%check%'
                OR name LIKE '%authorize%'
                OR name LIKE '%authenticate%')
        """)
        for r in mid_rows:
            results.append({
                "function": r["qualified_name"],
                "file": r["file_path"],
                "line": r["start_line"],
                "auth_level": "middleware",
                "roles": [],
                "permissions": [],
            })

        # Sort: authenticated first, then by file
        results.sort(key=lambda x: (
            0 if x["auth_level"] == "authenticated"
            else 1 if x["auth_level"] == "middleware"
            else 2 if x["auth_level"] == "public"
            else 3,
            x["file"],
        ))

        return results

    # ── 9. Heat map ─────────────────────────────────────────────────────

    def extract_heat_map(self) -> list[dict]:
        """Categorize functions by call frequency into hot/warm/cold.

        Hot  = top 10% of callers (high in-degree) — core infrastructure
        Warm = middle 40-90% — regular business logic
        Cold = bottom 40% — leaf functions, rarely called
        """
        G = self._get_call_graph()
        if G.number_of_nodes() == 0:
            return []

        # Compute in-degree (callers) for all functions
        func_degrees: list[tuple[str, int, int, int]] = []
        for node_id in G.nodes():
            in_d = G.in_degree(node_id)
            out_d = G.out_degree(node_id)
            func_degrees.append((node_id, in_d, out_d, in_d + out_d))

        if not func_degrees:
            return []

        # Sort by total degree descending
        func_degrees.sort(key=lambda x: -x[3])
        n = len(func_degrees)

        # Percentile thresholds
        hot_cutoff = max(1, int(n * 0.10))
        warm_cutoff = max(hot_cutoff + 1, int(n * 0.60))

        results: list[dict] = []
        categories = {"hot": 0, "warm": 0, "cold": 0}

        for idx, (node_id, in_d, out_d, total_d) in enumerate(func_degrees):
            if idx < hot_cutoff:
                heat = "hot"
            elif idx < warm_cutoff:
                heat = "warm"
            else:
                heat = "cold"

            func = self.reader.get_function_by_id(node_id)
            if func is None:
                continue

            categories[heat] += 1
            results.append({
                "name": func.name,
                "qualified_name": func.qualified_name,
                "file_path": func.file_path,
                "kind": func.kind,
                "heat": heat,
                "callers": in_d,
                "callees": out_d,
                "total_degree": total_d,
                "layer": self._classify_file_layer(func.file_path),
            })

        return results

    # ── Serialization helpers ───────────────────────────────────────────

    # ═══════════════════════════════════════════════════════════════════
    # Phase 3 — LLM-powered semantic understanding
    # ═══════════════════════════════════════════════════════════════════

    def _read_source_snippet(
        self, file_path: str, start_line: int, end_line: int, context_lines: int = 5
    ) -> str | None:
        """Read a function's source code from disk.

        Args:
            context_lines: extra lines before/after for context.
        """
        lo = max(1, start_line - context_lines)
        hi = end_line + context_lines
        return self.source_provider.snippet(file_path, lo, hi)

    def extract_business_descriptions(
        self, func_names: list[str] | None = None, limit: int = 20
    ) -> list[dict]:
        """Generate business-level descriptions for top functions via LLM.

        Args:
            func_names: Specific function qualified_names to explain (None = top by PageRank)
            limit: Max functions when using auto-selection
        """
        from .llm import batch_explain_functions, is_available as llm_ready

        if not llm_ready():
            return [{"error": "LLM not configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY."}]

        # Select functions to explain
        if func_names:
            funcs = []
            for qname in func_names:
                f = self.reader.get_function_by_qname(qname)
                if f:
                    funcs.append(f)
        else:
            entities = self.extract_core_entities(limit)
            funcs = []
            for e in entities:
                f = self.reader.get_function_by_id(e.node_id)
                if f:
                    funcs.append(f)

        if not funcs:
            return []

        # Build batch input
        batch = []
        for f in funcs:
            callees = self.reader.get_callees(f.node_id)
            callers = self.reader.get_callers(f.node_id)
            source = self._read_source_snippet(
                f.file_path, f.start_line, f.end_line
            )
            batch.append({
                "name": f.name,
                "qualified_name": f.qualified_name,
                "signature": f.signature,
                "docstring": None,  # CodeGraph captures this in the 'docstring' column
                "decorators": f.decorators,
                "file_path": f.file_path,
                "source_snippet": source,
                "callee_names": [c.callee_name for c in callees[:10]],
                "caller_names": [c.callee_name for c in callers[:5]],
            })

        return batch_explain_functions(batch)

    def extract_business_rules_llm(
        self, func_names: list[str] | None = None, limit: int = 15
    ) -> list[dict]:
        """Extract business rules from function bodies via LLM.

        Targets business-logic functions (identified by naming patterns
        and layer classification) rather than getters/setters/utilities.
        """
        from .llm import extract_business_rules, is_available as llm_ready

        if not llm_ready():
            return [{"error": "LLM not configured."}]

        # Select business-logic functions
        if func_names:
            funcs = [self.reader.get_function_by_qname(q) for q in func_names]
            funcs = [f for f in funcs if f is not None]
        else:
            # Heuristic: functions in application/domain layers, or with
            # business-sounding names (verbs, not get_/set_ prefixes)
            all_funcs = self.reader.get_all_functions()
            funcs = [
                f for f in all_funcs
                if not f.name.startswith(("get_", "set_", "__"))
                and self._classify_file_layer(f.file_path) in ("application", "domain", "")
                and not f.is_test
            ][:limit * 2]  # Over-sample, LLM will filter

        if not funcs:
            return []

        all_rules: list[dict] = []
        for f in funcs[:limit]:
            source = self._read_source_snippet(f.file_path, f.start_line, f.end_line)
            if not source:
                continue
            rules = extract_business_rules(
                func_name=f.qualified_name,
                source_snippet=source,
                file_path=f.file_path,
            )
            for r in rules:
                all_rules.append({
                    "function": r.function_name,
                    "rule_type": r.rule_type,
                    "description_en": r.description_en,
                    "description_zh": r.description_zh,
                    "condition": r.condition,
                    "failure_mode": r.failure_mode,
                })

        return all_rules

    def extract_error_catalog(
        self, func_names: list[str] | None = None, limit: int = 20
    ) -> list[dict]:
        """Extract error scenarios from function bodies via LLM."""
        from .llm import extract_error_scenarios, is_available as llm_ready

        if not llm_ready():
            return [{"error": "LLM not configured."}]

        if func_names:
            funcs = [self.reader.get_function_by_qname(q) for q in func_names]
            funcs = [f for f in funcs if f is not None]
        else:
            # Focus on entry points and hot functions (most likely to have errors)
            entities = self.extract_core_entities(30)
            funcs = []
            for e in entities:
                f = self.reader.get_function_by_id(e.node_id)
                if f and not f.is_test and not f.name.startswith("_"):
                    funcs.append(f)

        if not funcs:
            return []

        all_errors: list[dict] = []
        for f in funcs[:limit]:
            source = self._read_source_snippet(f.file_path, f.start_line, f.end_line)
            if not source:
                continue
            scenarios = extract_error_scenarios(
                func_name=f.qualified_name,
                source_snippet=source,
                file_path=f.file_path,
            )
            for s in scenarios:
                all_errors.append({
                    "function": s.function_name,
                    "error_type": s.error_type,
                    "trigger_condition": s.trigger_condition,
                    "handling": s.handling,
                    "user_facing": s.user_facing,
                })

        return all_errors

    def extract_state_machines(self) -> list[dict]:
        """Detect state machines from enum definitions + usage patterns via LLM."""
        from .llm import detect_state_machine, is_available as llm_ready

        if not llm_ready():
            return [{"error": "LLM not configured."}]

        # Find enum nodes from CodeGraph
        enum_nodes = self._query("""
            SELECT id, name, qualified_name, file_path, start_line, end_line
            FROM nodes
            WHERE kind = 'enum'
        """)

        if not enum_nodes:
            return []

        state_machines: list[dict] = []
        for enum in enum_nodes:
            # Find enum members
            members = self._query("""
                SELECT n.name FROM edges e
                JOIN nodes n ON n.id = e.target
                WHERE e.source = ? AND e.kind = 'contains' AND n.kind = 'enum_member'
            """, [enum["id"]])

            member_names = [m["name"] for m in members]
            if len(member_names) < 2:
                continue  # Need at least 2 states

            # Skip non-state enums (heuristic: state-related naming)
            enum_name = enum["name"].lower()
            if not any(kw in enum_name for kw in ("status", "state", "stage", "phase", "type")):
                continue

            # Find functions that reference this enum's members
            ref_funcs: list[dict] = []
            for member_name in member_names[:20]:
                rows = self._query("""
                    SELECT DISTINCT n.qualified_name, n.file_path, n.start_line, n.end_line
                    FROM nodes n
                    WHERE n.name LIKE ? AND n.kind IN ('function', 'method')
                """, [f"%{member_name}%"])
                for r in rows:
                    if r["qualified_name"] not in {f.get("name") for f in ref_funcs}:
                        ref_funcs.append({
                            "name": r["qualified_name"],
                            "file_path": r["file_path"],
                            "start_line": r["start_line"],
                            "end_line": r["end_line"],
                        })

            if not ref_funcs:
                continue

            # Read snippets for context
            for rf in ref_funcs[:8]:
                source = self._read_source_snippet(
                    rf["file_path"], rf["start_line"], rf["end_line"]
                )
                rf["relevant_lines"] = (
                    self._extract_enum_usage_lines(source, member_names)
                    if source else ""
                )

            sm = detect_state_machine(
                entity_name=enum["name"],
                enum_name=enum["qualified_name"],
                enum_members=member_names,
                transition_functions=ref_funcs,
            )
            if sm and sm.states:
                state_machines.append({
                    "entity": sm.entity,
                    "states": sm.states,
                    "initial_state": sm.initial_state,
                    "terminal_states": sm.terminal_states,
                    "transitions": sm.transitions,
                })

        return state_machines

    @staticmethod
    def _extract_enum_usage_lines(source: str | None, member_names: list[str]) -> str:
        """Extract lines from source that reference enum members."""
        if not source:
            return ""
        relevant = []
        for line in source.split("\n"):
            line_stripped = line.strip()
            for m in member_names:
                if m in line_stripped:
                    relevant.append(line_stripped)
                    break
        return "\n".join(relevant[:15])

    @staticmethod
    def _serialize_config_consumption(data: list[dict]) -> dict:
        total_keys = sum(d["key_count"] for d in data)
        total_consumed = sum(d["consumed_keys"] for d in data)
        return {
            "config_files": len(data),
            "total_keys": total_keys,
            "consumed_keys": total_consumed,
            "files": data[:20],
        }

    @staticmethod
    def _serialize_external_deps(data: list[dict]) -> dict:
        total_deps = sum(d["dependency_count"] for d in data)
        return {
            "categories": len(data),
            "total_dependencies": total_deps,
            "by_category": data,
        }

    @staticmethod
    def _serialize_auth_model(data: list[dict]) -> dict:
        endpoints = [d for d in data if d["auth_level"] not in ("middleware",)]
        roles = sorted(set(r for d in data for r in d["roles"]))
        perms = sorted(set(p for d in data for p in d["permissions"]))
        return {
            "protected_endpoints": len(endpoints),
            "middleware_count": len(data) - len(endpoints),
            "roles": roles,
            "permissions": perms,
            "entries": data[:100],
        }

    @staticmethod
    def _serialize_heat_map(data: list[dict]) -> dict:
        counts = defaultdict(int)
        for d in data:
            counts[d["heat"]] += 1
        return {
            "total_functions": len(data),
            "hot": counts["hot"],
            "warm": counts["warm"],
            "cold": counts["cold"],
            "hot_functions": [d for d in data if d["heat"] == "hot"][:30],
            "warm_functions": [d for d in data if d["heat"] == "warm"][:30],
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
        return self.reader.query(sql, params)
