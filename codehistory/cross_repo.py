"""Cross-repo microservice topology — stitch services into a unified graph.

P0 capabilities:
  1. Extract outbound HTTP calls from each service (via CodeGraph edges)
  2. Match outbound URLs against inbound route templates across services
  3. Build unified service dependency topology
  4. Trace end-to-end call chains across service boundaries
  5. Cross-service change impact analysis

All reads from each service's `.codegraph/codegraph.db` SQLite.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .analysis.topology.matching import PathMatcher
from .infrastructure.codegraph_sqlite import read_rows

# ── HTTP client call patterns per language ─────────────────────────────

# (function_name_pattern, http_method_inference)
HTTP_CLIENT_CALLERS: dict[str, list[tuple[str, str]]] = {
    "python": [
        ("requests.get", "GET"),
        ("requests.post", "POST"),
        ("requests.put", "PUT"),
        ("requests.delete", "DELETE"),
        ("requests.patch", "PATCH"),
        ("requests.head", "HEAD"),
        ("requests.request", None),  # method is first arg
        ("httpx.get", "GET"),
        ("httpx.post", "POST"),
        ("httpx.put", "PUT"),
        ("httpx.delete", "DELETE"),
        ("httpx.patch", "PATCH"),
        ("urllib.request.urlopen", None),
        ("urllib3.PoolManager.request", None),
        ("aiohttp.ClientSession.get", "GET"),
        ("aiohttp.ClientSession.post", "POST"),
    ],
    "javascript": [
        ("fetch", None),  # method is in options
        ("axios.get", "GET"),
        ("axios.post", "POST"),
        ("axios.put", "PUT"),
        ("axios.delete", "DELETE"),
        ("axios.patch", "PATCH"),
        ("got(", "GET"),  # got.post, got.get etc. — check callee name
    ],
    "typescript": [
        ("fetch", None),
        ("axios.get", "GET"),
        ("axios.post", "POST"),
        ("axios.put", "PUT"),
        ("axios.delete", "DELETE"),
        ("axios.patch", "PATCH"),
        ("got(", "GET"),
    ],
    "java": [
        ("RestTemplate.getForObject", "GET"),
        ("RestTemplate.postForObject", "POST"),
        ("RestTemplate.put", "PUT"),
        ("RestTemplate.delete", "DELETE"),
        ("RestTemplate.exchange", None),
        ("WebClient.get", "GET"),
        ("WebClient.post", "POST"),
        ("WebClient.put", "PUT"),
        ("WebClient.delete", "DELETE"),
        ("HttpClient.send", None),
        ("OkHttpClient.newCall", None),
        ("HttpURLConnection", None),
    ],
    "go": [
        ("http.Get", "GET"),
        ("http.Post", "POST"),
        ("http.PostForm", "POST"),
        ("http.Head", "HEAD"),
        ("http.NewRequest", None),
        ("http.NewRequestWithContext", None),
    ],
    "rust": [
        ("reqwest::get", "GET"),
        ("reqwest::Client.get", "GET"),
        ("reqwest::Client.post", "POST"),
        ("reqwest::Client.put", "PUT"),
        ("reqwest::Client.delete", "DELETE"),
        ("ureq::get", "GET"),
        ("ureq::post", "POST"),
    ],
    "ruby": [
        ("Net::HTTP.get", "GET"),
        ("Net::HTTP.post", "POST"),
        ("Faraday.get", "GET"),
        ("Faraday.post", "POST"),
        ("HTTParty.get", "GET"),
        ("HTTParty.post", "POST"),
    ],
}


# ── Output types ───────────────────────────────────────────────────────


@dataclass
class OutboundCall:
    """An HTTP call made by a function to another service."""

    caller_service: str
    caller_function: str  # qualified_name
    caller_file: str
    caller_line: int
    http_method: str | None  # GET/POST/PUT/DELETE or None (inferred from context)
    url_or_pattern: str  # raw URL string from source if extractable
    callee_name: str  # the HTTP client function name


@dataclass
class CrossServiceEdge:
    """A matched cross-service call."""

    source_service: str
    source_function: str
    source_file: str
    source_line: int
    target_service: str
    target_function: str  # handler qualified_name
    target_file: str
    target_line: int
    http_method: str
    url_pattern: str  # matched route template, e.g. /api/users/:id
    raw_url: str  # raw URL from the caller


@dataclass
class ServiceNode:
    """A service in the unified topology."""

    name: str
    repo_path: str
    language: str
    role: str = ""  # gateway/backend/worker/cron/unknown
    apis: list[dict] = field(default_factory=list)  # inbound API list
    outbound_calls: list[dict] = field(default_factory=list)  # outbound HTTP calls
    dependencies: list[str] = field(default_factory=list)  # other service names
    db_type: str = ""  # postgres/mysql/mongodb/redis/...
    mq_type: str = ""  # kafka/rabbitmq/nats/...


@dataclass
class UnifiedTopology:
    """The complete multi-service call topology."""

    services: list[ServiceNode]
    cross_edges: list[CrossServiceEdge]
    # adjacency: service → [dependent_service_names]
    dependency_graph: dict[str, list[str]] = field(default_factory=dict)
    # service pairs without cross-service edges but with matching URL patterns
    potential_edges: list[dict] = field(default_factory=list)


# ── Cross-repo analyzer ────────────────────────────────────────────────


class CrossRepoAnalyzer:
    """Analyzes multiple code repos as a unified microservice system.

    Usage:
        analyzer = CrossRepoAnalyzer([
            {"name": "order-service", "path": "/repos/order-svc"},
            {"name": "user-service",  "path": "/repos/user-svc"},
        ])
        topology = analyzer.analyze()
        print(analyzer.format_topology(topology))
    """

    def __init__(self, repos: list[dict]):
        """
        Args:
            repos: [{"name": ..., "path": ...}, ...]
                   Each repo must have `codegraph init` run first.
        """
        self.repos = repos

    def _cg_db(self, repo_path: str) -> str:
        return str(Path(repo_path) / ".codegraph" / "codegraph.db")

    def _query(self, db_path: str, sql: str, params: list | None = None) -> list[dict]:
        """Run a read-only query against a CodeGraph SQLite."""
        return read_rows(db_path, sql, params)

    # ── Analysis pipeline ───────────────────────────────────────────────

    def analyze(self) -> UnifiedTopology:
        """Run the full multi-service analysis."""
        services: list[ServiceNode] = []
        all_outbound: list[OutboundCall] = []
        all_inbound: dict[str, list[dict]] = {}  # service_name → [api_endpoint]

        for repo in self.repos:
            db_path = self._cg_db(repo["path"])
            if not Path(db_path).exists():
                print(f"  [skip] {repo['name']}: no CodeGraph DB at {db_path}")
                continue

            svc = self._analyze_service(repo["name"], repo["path"], db_path)
            services.append(svc)

            outbound = self._extract_outbound_calls(repo["name"], db_path)
            all_outbound.extend(outbound)

            # We'll collect inbound APIs from each service for matching
            from .codegraph_reader import CodeGraphReader
            from .knowledge import KnowledgeExtractor

            reader = CodeGraphReader(db_path)
            ke = KnowledgeExtractor(reader)
            api = ke.extract_api_contract()
            all_inbound[repo["name"]] = [
                {
                    "method": ep.method,
                    "path": ep.path,
                    "handler": ep.handler_name,
                    "file": ep.file_path,
                    "line": ep.line,
                }
                for ep in api.endpoints
            ]
            reader.close()

        # Match outbound calls to inbound APIs
        cross_edges = self._match_cross_edges(all_outbound, all_inbound)

        # Build dependency graph
        dep_graph: dict[str, list[str]] = defaultdict(list)
        for edge in cross_edges:
            if edge.target_service not in dep_graph[edge.source_service]:
                dep_graph[edge.source_service].append(edge.target_service)

        # Annotate services with dependencies
        for svc in services:
            svc.apis = all_inbound.get(svc.name, [])
            svc.dependencies = dep_graph.get(svc.name, [])

        # Find potential unmatched edges
        potential = self._find_potential_edges(all_outbound, all_inbound, cross_edges)

        return UnifiedTopology(
            services=services,
            cross_edges=cross_edges,
            dependency_graph=dict(dep_graph),
            potential_edges=potential,
        )

    # ── Service analysis ────────────────────────────────────────────────

    def _analyze_service(self, name: str, path: str, db_path: str) -> ServiceNode:
        """Classify a service by its tech stack and role."""
        # Detect primary language
        lang_rows = self._query(
            db_path,
            """
            SELECT language, COUNT(*) AS cnt FROM files
            WHERE language != 'unknown'
            GROUP BY language ORDER BY cnt DESC LIMIT 1
        """,
        )
        language = lang_rows[0]["language"] if lang_rows else "unknown"

        # Detect service role by path patterns
        role = self._infer_role(path, db_path)

        # Detect infrastructure
        db_type = self._detect_db(db_path)
        mq_type = self._detect_mq(db_path)

        return ServiceNode(
            name=name,
            repo_path=path,
            language=language,
            role=role,
            db_type=db_type,
            mq_type=mq_type,
        )

    @staticmethod
    def _infer_role(repo_path: str, db_path: str) -> str:
        """Infer service role from directory/file naming patterns."""
        path_lower = repo_path.lower()
        if any(k in path_lower for k in ("gateway", "proxy", "bff", "ingress")):
            return "gateway"
        if any(k in path_lower for k in ("worker", "consumer", "job", "cron", "scheduler")):
            return "worker"
        if any(k in path_lower for k in ("cron", "scheduler", "timer")):
            return "cron"
        return "backend"

    def _detect_db(self, db_path: str) -> str:
        """Detect database type from imports/decorators."""
        db_patterns = {
            "postgres": ("postgres", "psycopg", "pg_", "postgresql"),
            "mysql": ("mysql", "mariadb"),
            "mongodb": ("mongo", "pymongo", "mongoose"),
            "redis": ("redis", "aioredis"),
            "sqlite": ("sqlite",),
        }
        for db_type, patterns in db_patterns.items():
            for p in patterns:
                rows = self._query(
                    db_path,
                    """
                    SELECT 1 FROM nodes WHERE name LIKE ? LIMIT 1
                """,
                    [f"%{p}%"],
                )
                if rows:
                    return db_type
        return ""

    def _detect_mq(self, db_path: str) -> str:
        """Detect message queue type."""
        mq_patterns = {
            "kafka": ("kafka",),
            "rabbitmq": ("rabbitmq", "amqp", "pika"),
            "nats": ("nats", "stan"),
            "sqs": ("sqs",),
            "pubsub": ("pubsub",),
            "celery": ("celery",),
        }
        for mq, patterns in mq_patterns.items():
            for p in patterns:
                rows = self._query(
                    db_path,
                    """
                    SELECT 1 FROM nodes WHERE name LIKE ? LIMIT 1
                """,
                    [f"%{p}%"],
                )
                if rows:
                    return mq
        return ""

    # ── Outbound HTTP call extraction ───────────────────────────────────

    def _extract_outbound_calls(self, service_name: str, db_path: str) -> list[OutboundCall]:
        """Extract all outbound HTTP calls from a service's call graph."""
        results: list[OutboundCall] = []

        # Get the language pattern set for this service
        # First detect language
        lang_row = self._query(
            db_path,
            """
            SELECT language FROM files
            WHERE language != 'unknown'
            GROUP BY language ORDER BY COUNT(*) DESC LIMIT 1
        """,
        )
        language = lang_row[0]["language"] if lang_row else ""
        patterns = HTTP_CLIENT_CALLERS.get(language, [])

        if not patterns:
            return results

        # Find all call edges where the callee matches an HTTP client pattern
        for pattern, method in patterns:
            rows = self._query(
                db_path,
                """
                SELECT n1.name AS caller_name, n1.qualified_name AS caller_qname,
                       n1.file_path, n1.start_line AS caller_line,
                       n2.name AS callee_name,
                       e.line AS call_line
                FROM edges e
                JOIN nodes n1 ON n1.id = e.source
                JOIN nodes n2 ON n2.id = e.target
                WHERE e.kind = 'calls'
                  AND n2.name LIKE ?
            """,
                [f"%{pattern}%"],
            )

            for r in rows:
                # Try to extract URL from the call context
                url = self._extract_url_from_context(db_path, r["caller_qname"], r["call_line"])

                results.append(
                    OutboundCall(
                        caller_service=service_name,
                        caller_function=r["caller_qname"],
                        caller_file=r["file_path"],
                        caller_line=r["caller_line"],
                        http_method=method,
                        url_or_pattern=url or "",
                        callee_name=r["callee_name"],
                    )
                )

        return results

    def _extract_url_from_context(
        self, db_path: str, caller_qname: str, call_line: int | None
    ) -> str | None:
        """Extract URL from near the HTTP call site by reading source code.

        Strategy (in order):
          1. Read source lines around the call line, find URL patterns in the
             actual call expression (f-strings, template literals, concatenation)
          2. Look for variable assignments on preceding lines that look like URLs
          3. Fall back to variable/constant node name matching
        """
        if call_line is None:
            return None

        # Find the caller context
        caller_row = self._query(
            db_path,
            """
            SELECT file_path, start_line, end_line FROM nodes
            WHERE qualified_name = ? AND kind IN ('function', 'method')
        """,
            [caller_qname],
        )
        if not caller_row:
            return None

        c = caller_row[0]
        file_path = c["file_path"]

        # Strategy 1: read source around the call line
        source_url = self._extract_url_from_source(file_path, call_line, db_path)
        if source_url:
            return source_url

        # Strategy 2: look for variable assignments on preceding lines
        func_start = c["start_line"]
        func_end = c["end_line"]
        candidates = self._query(
            db_path,
            """
            SELECT name, start_line FROM nodes
            WHERE file_path = ?
              AND start_line BETWEEN ? AND ?
              AND kind IN ('variable', 'constant')
              AND (
                name LIKE '%http%' OR name LIKE '%://%' OR name LIKE '%/api/%'
                OR name LIKE '%base_url%' OR name LIKE '%endpoint%'
                OR name LIKE '%host%' OR name LIKE '%service_url%'
              )
            ORDER BY start_line
        """,
            [file_path, func_start, func_end],
        )

        for cand in candidates:
            name = cand["name"]
            url_match = re.search(r'(?:https?://[^\s\'",;]+|/[a-z]+/[^\s\'",;]+)', name)
            if url_match:
                return url_match.group(0)

        return None

    def _extract_url_from_source(self, file_path: str, call_line: int, db_path: str) -> str | None:
        """Read source code around the call line and extract URL from arguments.

        Handles:
          - f-strings: f"http://{host}/api/users/{id}"
          - template literals: `http://${host}/api/users/${id}`
          - string concatenation: "http://" + host + "/api/users/" + id
          - plain strings: "http://user-service/api/users/123"
          - variable references where the variable is a URL
        """
        # Read ~10 lines around the call
        # Actually, we need the actual repo path. Let's derive from db_path.
        repo_root = str(Path(db_path).parent.parent)
        source_path = str(Path(repo_root) / file_path)
        try:
            with open(source_path, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except (OSError, FileNotFoundError):
            return None

        if call_line < 1 or call_line > len(lines):
            return None

        # Collect context: ~5 lines before through 3 lines after the call
        start = max(0, call_line - 6)
        end = min(len(lines), call_line + 3)
        context = "".join(lines[start:end])

        # The call line itself
        call_text = lines[call_line - 1].strip()

        # Pattern 1: f-string / template literal with URL
        # f"http://{host}/api/users/{id}" or f'http://{host}/api/users/{id}'
        for pat in [
            r"""f["'](https?://[^"'{]+)""",
            r"""f['"](https?://[^'"}{]+)""",
        ]:
            m = re.search(pat, context)
            if m:
                return m.group(1).rstrip("/")

        # Pattern 2: JS template literal `http://${host}/...`
        m = re.search(r"`(https?://[^`]+)`", context)
        if m:
            return m.group(1).rstrip("/")

        # Pattern 3: plain quoted URL string
        for pat in [
            r"""['\"](https?://[a-zA-Z0-9._:-]+(?:/[^\s"'*,;)]*)?)['\"]""",
            r"""['"](https?://[a-zA-Z0-9._-]+(?:/[^\s"'*,;)]*)?)['"]""",
        ]:
            m = re.search(pat, context)
            if m:
                return m.group(1).rstrip("/")

        # Pattern 4: string concatenation — "http://" + host + "/api/users/" + id
        m = re.search(
            r"""["'](https?://)["']\s*\+\s*([^+]+?)\s*\+\s*["']((?:/[^"']*)?)["']""", context
        )
        if m:
            # Return f-string style: keep the concat as readable URL
            return m.group(1) + "..." + (m.group(3) or "")

        # Pattern 5: URL constructed from a constant variable
        # Look for variable names like USER_SERVICE_URL, API_BASE, etc.
        m = re.search(
            r"(?:requests\.\w+|fetch|axios\.\w+|httpx\.\w+)\((?:"
            r'f["\']?(https?://[^"\'{]+)|'
            r'["\']?(https?://[^"\'{]+)|'
            r"(\w+(?:_URL|_HOST|_ENDPOINT|_BASE))"
            r")",
            call_text,
        )
        if m:
            return m.group(1) or m.group(2) or m.group(3) or ""

        # Pattern 6: URL path-only pattern (relative URL)
        m = re.search(r"""["'](/api/[^\s"'*,;)]+)["']""", context)
        if m:
            return m.group(1)

        return None

    # ── Cross-service edge matching ─────────────────────────────────────

    def _match_cross_edges(
        self,
        outbound: list[OutboundCall],
        inbound: dict[str, list[dict]],
    ) -> list[CrossServiceEdge]:
        """Match outbound HTTP calls to inbound API endpoints across services."""
        edges: list[CrossServiceEdge] = []

        for call in outbound:
            url = call.url_or_pattern
            if not url:
                continue

            method = call.http_method

            # Extract path from URL
            path = self._extract_path(url)
            if not path:
                continue

            # Try to match against each service's inbound APIs
            for svc_name, apis in inbound.items():
                if svc_name == call.caller_service:
                    continue  # skip self-calls

                for api in apis:
                    api_method = api.get("method", "")
                    api_path = api.get("path", "")

                    # Method must match (or be unknown)
                    if method and api_method and method.upper() != api_method.upper():
                        continue

                    # Path matching
                    if self._paths_match(path, api_path):
                        edges.append(
                            CrossServiceEdge(
                                source_service=call.caller_service,
                                source_function=call.caller_function,
                                source_file=call.caller_file,
                                source_line=call.caller_line,
                                target_service=svc_name,
                                target_function=api.get("handler", ""),
                                target_file=api.get("file", ""),
                                target_line=api.get("line", 0),
                                http_method=api_method or method or "UNKNOWN",
                                url_pattern=api_path,
                                raw_url=url,
                            )
                        )
                        break  # first match wins

        return edges

    @staticmethod
    def _extract_path(url: str) -> str:
        """Extract the path component from a URL string.

        Handles raw URLs, f-strings, template strings, and variables.
        """
        return PathMatcher.extract(url)

    @staticmethod
    def _paths_match(actual_or_template: str, route_template: str) -> bool:
        """Check if two URL paths match.

        Examples:
          /api/users/123        vs /api/users/:id      → True
          /api/users/:param     vs /api/users/:id      → True
          /api/users/123        vs /api/orders/:id     → False
          /api/users/123/posts  vs /api/users/:id/posts → True
        """
        return PathMatcher.matches(actual_or_template, route_template)

    # ── Potential edge discovery ────────────────────────────────────────

    def _find_potential_edges(
        self,
        outbound: list[OutboundCall],
        inbound: dict[str, list[dict]],
        matched: list[CrossServiceEdge],
    ) -> list[dict]:
        """Find outbound calls that COULD be cross-service but didn't match.

        Useful for surfacing calls that might be to external services
        or where URL extraction failed to produce a matchable pattern.
        """
        matched_callers = {(e.source_service, e.source_function, e.raw_url) for e in matched}

        potential = []
        for call in outbound:
            key = (call.caller_service, call.caller_function, call.url_or_pattern)
            if key in matched_callers:
                continue

            url = call.url_or_pattern
            if not url:
                continue

            # Guess target service from URL hostname
            host = self._extract_host(url)
            if host:
                potential.append(
                    {
                        "source_service": call.caller_service,
                        "source_function": call.caller_function,
                        "source_file": call.caller_file,
                        "source_line": call.caller_line,
                        "http_method": call.http_method,
                        "url": url,
                        "suspected_target": host,
                        "reason": "URL hostname matched a known service name"
                        if any(r["name"] in host for r in self.repos)
                        else "Possible external service",
                    }
                )

        return potential

    @staticmethod
    def _extract_host(url: str) -> str:
        """Extract hostname from a URL string."""
        m = re.search(r'://([^/\'",;?#]+)', url)
        return m.group(1) if m else ""

    # ── Impact analysis ─────────────────────────────────────────────────

    def impact_analysis(self, topology: UnifiedTopology, changed_service: str) -> dict:
        """Analyze which services are affected by a change to a given service.

        Returns upstream + downstream impact.
        """
        dep_graph = topology.dependency_graph

        # Downstream: services that THIS service calls
        downstream = dep_graph.get(changed_service, [])

        # Upstream: services that call THIS service
        upstream = [svc for svc, deps in dep_graph.items() if changed_service in deps]

        # Affected edges (cross-service edges involving this service)
        affected_edges = [
            e
            for e in topology.cross_edges
            if e.source_service == changed_service or e.target_service == changed_service
        ]

        return {
            "service": changed_service,
            "upstream_impact": upstream,  # who calls us
            "downstream_impact": downstream,  # who we call
            "affected_cross_edges": [
                {
                    "from": f"{e.source_service}::{e.source_function}",
                    "to": f"{e.target_service}::{e.target_function}",
                    "method": e.http_method,
                    "url": e.url_pattern,
                }
                for e in affected_edges
            ],
        }

    # ── End-to-end trace ────────────────────────────────────────────────

    def trace_flow(
        self,
        topology: UnifiedTopology,
        start_service: str,
        start_api_path: str | None = None,
        max_depth: int = 5,
    ) -> list[dict]:
        """Trace an end-to-end flow starting from a service's API.

        Args:
            start_service: The entry point service name.
            start_api_path: Specific API path to start from (None = all APIs).
            max_depth: Max cross-service hops.
        """
        # Find matching edges from start_service
        edges = [e for e in topology.cross_edges if e.source_service == start_service]
        if start_api_path:
            edges = [e for e in edges if e.url_pattern == start_api_path]

        visited_edges: set[tuple[str, str, str]] = set()
        chain: list[dict] = []

        def follow(service: str, depth: int, incoming_path: str = ""):
            if depth > max_depth:
                return

            # Get all outbound edges from this service
            outgoing = [e for e in topology.cross_edges if e.source_service == service]

            for e in outgoing:
                edge_key = (e.source_service, e.target_service, e.url_pattern)
                if edge_key in visited_edges:
                    continue
                visited_edges.add(edge_key)

                chain.append(
                    {
                        "depth": depth,
                        "from_service": e.source_service,
                        "from_function": e.source_function,
                        "to_service": e.target_service,
                        "to_function": e.target_function,
                        "method": e.http_method,
                        "url": e.url_pattern,
                    }
                )

                # Follow into the target service
                follow(e.target_service, depth + 1, e.url_pattern)

        follow(start_service, 0)
        return chain

    # ── Formatters ──────────────────────────────────────────────────────

    def format_topology(self, t: UnifiedTopology) -> str:
        """Render the unified topology as text."""
        lines = []
        lines.append(f"{'=' * 70}")
        lines.append(
            f"Unified Topology: {len(t.services)} services, {len(t.cross_edges)} cross-service edges"
        )
        lines.append(f"{'=' * 70}")

        # Service list
        lines.append("\nServices:")
        for svc in t.services:
            extras = []
            if svc.role:
                extras.append(svc.role)
            if svc.db_type:
                extras.append(svc.db_type)
            if svc.mq_type:
                extras.append(svc.mq_type)
            extra_str = f" ({', '.join(extras)})" if extras else ""
            lines.append(
                f"  [{svc.language:6s}] {svc.name:20s}{extra_str}"
                f"  APIs={len(svc.apis)}  deps={svc.dependencies}"
            )

        # Dependency graph
        if t.dependency_graph:
            lines.append("\nDependency Graph:")
            for svc, deps in sorted(t.dependency_graph.items()):
                for d in deps:
                    lines.append(f"  {svc} → {d}")

        # Cross-service edges
        if t.cross_edges:
            lines.append(f"\nCross-Service Edges ({len(t.cross_edges)}):")
            for e in t.cross_edges[:30]:
                lines.append(
                    f"  {e.source_service}::{e.source_function.split('::')[-1]}"
                    f"  ──[{e.http_method} {e.url_pattern}]──→"
                    f"  {e.target_service}::{e.target_function.split('::')[-1]}"
                )
            if len(t.cross_edges) > 30:
                lines.append(f"  ... and {len(t.cross_edges) - 30} more")

        # Potential edges
        if t.potential_edges:
            lines.append(f"\nPotential External Dependencies ({len(t.potential_edges)}):")
            for p in t.potential_edges[:15]:
                lines.append(
                    f"  [{p['suspected_target']}] {p['source_service']}::{p['source_function'].split('::')[-1]}"
                    f"  → {p.get('http_method', '?')} {p.get('url', '')}"
                    f"  ({p.get('reason', '')})"
                )

        return "\n".join(lines)

    def format_impact(self, impact: dict) -> str:
        """Render impact analysis as text."""
        lines = [
            f"{'=' * 60}",
            f"Impact Analysis: {impact['service']}",
            f"{'=' * 60}",
            f"  Upstream (who calls us):   {impact['upstream_impact']}",
            f"  Downstream (who we call):  {impact['downstream_impact']}",
            f"  Affected cross-edges: {len(impact['affected_cross_edges'])}",
        ]
        for e in impact["affected_cross_edges"][:15]:
            lines.append(f"    {e['from']}  ──[{e['method']} {e['url']}]──→  {e['to']}")
        return "\n".join(lines)

    def format_trace(self, chain: list[dict]) -> str:
        """Render an end-to-end trace as text."""
        if not chain:
            return "No cross-service flow found."

        lines = [f"{'=' * 60}", "End-to-End Flow Trace", f"{'=' * 60}"]
        for step in chain:
            indent = "  " * step["depth"]
            lines.append(
                f"{indent}[{step['method']} {step['url']}]"
                f"  {step['from_service']}::{step['from_function'].split('::')[-1]}"
                f"  →  {step['to_service']}::{step['to_function'].split('::')[-1]}"
            )
        return "\n".join(lines)
