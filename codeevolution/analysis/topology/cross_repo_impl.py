"""Cross-repo microservice topology — stitch services into a unified graph.

P0 capabilities:
  1. Traverse CodeGraph calls from inbound endpoint handlers
  2. Extract endpoint-reachable outbound HTTP calls
  3. Match outbound URLs against inbound route templates across services
  4. Build unified service dependency topology
  5. Trace end-to-end call chains across service boundaries
  6. Cross-service change impact analysis

All reads from each service's `.codegraph/codegraph.db` SQLite.
"""

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from ...infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository
from .flow import FlowTracer
from .impact import ImpactAnalyzer
from .matching import PathMatcher
from .rules import TopologyRuleSet

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


# ── URL extraction helpers (module-level, reusable by the call-chain tree) ─


def extract_outbound_url(db_path: str, caller_qname: str, call_line: int | None) -> str | None:
    """Extract URL from near an HTTP call site by reading source code.

    Reusable per-call heuristic shared by whole-service topology scans and the
    per-endpoint call-chain tree. Strategy (in order):
      1. Read source lines around the call line, find URL patterns in the
         actual call expression (f-strings, template literals, concatenation)
      2. Look for variable assignments on preceding lines that look like URLs
      3. Fall back to variable/constant node name matching
    """
    if call_line is None:
        return None

    # Find the caller context
    with SQLiteCodeGraphRepository(db_path) as repository:
        caller_row = repository.function_location(caller_qname)
    if not caller_row:
        return None

    c = caller_row[0]
    file_path = c["file_path"]

    # Strategy 1: read source around the call line
    source_url = _url_from_source_lines(file_path, call_line, db_path)
    if source_url:
        return source_url

    # Strategy 2: look for variable assignments on preceding lines
    func_start = c["start_line"]
    func_end = c["end_line"]
    with SQLiteCodeGraphRepository(db_path) as repository:
        candidates = repository.url_candidate_nodes(file_path, func_start, func_end)

    for cand in candidates:
        name = cand["name"]
        url_match = re.search(r'(?:https?://[^\s\'",;]+|/[a-z]+/[^\s\'",;]+)', name)
        if url_match:
            return url_match.group(0)

    return None


def _url_from_source_lines(file_path: str, call_line: int, db_path: str) -> str | None:
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
    source_endpoint_method: str = ""
    source_endpoint_path: str = ""
    source_endpoint_handler: str = ""
    call_chain: list[dict] = field(default_factory=list)


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
    match_rule: str = "http-method+path-template"
    confidence: float = 1.0
    evidence: dict = field(default_factory=dict)
    rule_version: str = "http-static-v1"
    source_endpoint_method: str = ""
    source_endpoint_path: str = ""
    source_endpoint_handler: str = ""
    call_chain: list[dict] = field(default_factory=list)


@dataclass
class MessageDependencyEdge:
    """An asynchronous producer-to-consumer service dependency."""

    source_service: str
    source_function: str
    target_service: str
    target_function: str
    broker_type: str
    channel: str
    source_endpoint_method: str = ""
    source_endpoint_path: str = ""
    source_endpoint_handler: str = ""
    source_entry_kind: str = "http"
    call_chain: list[dict] = field(default_factory=list)
    confidence: float = 0.9
    match_rule: str = "message-channel-name"
    evidence: dict = field(default_factory=dict)
    rule_version: str = "message-static-v1"


@dataclass
class ResourceDependencyEdge:
    """A service-to-infrastructure dependency, not a service call."""

    source_service: str
    source_function: str
    resource_type: str
    resource_id: str
    operation: str
    resource_key: str = ""
    source_endpoint_method: str = ""
    source_endpoint_path: str = ""
    source_endpoint_handler: str = ""
    source_entry_kind: str = "http"
    call_chain: list[dict] = field(default_factory=list)
    confidence: float = 0.8
    match_rule: str = "resource-client-call"
    evidence: dict = field(default_factory=dict)
    rule_version: str = "resource-static-v1"


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
    message_edges: list[MessageDependencyEdge] = field(default_factory=list)
    resource_edges: list[ResourceDependencyEdge] = field(default_factory=list)
    resource_dependency_graph: dict[str, list[str]] = field(default_factory=dict)


# ── Cross-repo analyzer ────────────────────────────────────────────────


class CrossRepoImplementation:
    """Analyzes multiple code repos as a unified microservice system.

    Usage:
        analyzer = CrossRepoAnalyzer([
            {"name": "order-service", "path": "/repos/order-svc"},
            {"name": "user-service",  "path": "/repos/user-svc"},
        ])
        topology = analyzer.analyze()
        print(analyzer.format_topology(topology))
    """

    def __init__(self, repos: list[dict], rules: TopologyRuleSet | None = None):
        """
        Args:
            repos: [{"name": ..., "path": ...}, ...]
                   Each repo must have `codegraph init` run first.
        """
        self.repos = repos
        self.rules = rules or TopologyRuleSet(http_client_callers=HTTP_CLIENT_CALLERS)
        self._service_cache: dict[tuple[str, str], tuple[tuple[int, int], ServiceNode, list, list]] = {}
        self.cache_stats = {"hits": 0, "misses": 0}

    def _cg_db(self, repo_path: str) -> str:
        return str(Path(repo_path) / ".codegraph" / "codegraph.db")

    # ── Analysis pipeline ───────────────────────────────────────────────

    def analyze(self) -> UnifiedTopology:
        """Run the full multi-service analysis."""
        self.cache_stats = {"hits": 0, "misses": 0}
        services_by_name: dict[str, ServiceNode] = {}
        all_outbound: list[OutboundCall] = []
        all_inbound: dict[str, list[dict]] = {}  # service_name → [api_endpoint]

        repositories = [
            {**member, "name": service["name"]}
            for service in self.repos
            for member in service.get("repositories", [service])
        ]
        for repo in repositories:
            db_path = self._cg_db(repo["path"])
            if not Path(db_path).exists():
                print(f"  [skip] {repo['name']}: no CodeGraph DB at {db_path}")
                continue

            cache_key = (repo["name"], repo["path"])
            stat = Path(db_path).stat()
            fingerprint = (stat.st_mtime_ns, stat.st_size)
            cached = self._service_cache.get(cache_key)
            if cached and cached[0] == fingerprint:
                _, svc, outbound, inbound = cached
                self._merge_service(services_by_name, svc)
                all_outbound.extend(outbound)
                all_inbound.setdefault(repo["name"], []).extend(inbound)
                self.cache_stats["hits"] += 1
                continue
            self.cache_stats["misses"] += 1

            # We'll collect inbound APIs from each service for matching
            from ...analysis.knowledge.api_contract import ApiContractExtractor
            from ...infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository

            with SQLiteCodeGraphRepository(db_path) as reader:
                api = ApiContractExtractor(reader).extract()
                inbound = []
                for ep in api.endpoints:
                    handler_node = (
                        reader.function_node(ep.handler_name) if ep.handler_name else None
                    )
                    inbound.append(
                        {
                            "method": ep.method,
                            "path": ep.path,
                            "handler": ep.handler_name,
                            "file": ep.file_path,
                            "line": ep.line,
                            "node_id": handler_node["id"] if handler_node else "",
                        }
                    )

            outbound = self._extract_outbound_calls(repo["name"], db_path, inbound)
            all_outbound.extend(outbound)

            svc = self._analyze_service(repo["name"], repo["path"], db_path)
            self._merge_service(services_by_name, svc)
            all_inbound.setdefault(repo["name"], []).extend(inbound)
            self._service_cache[cache_key] = (
                fingerprint,
                svc,
                outbound,
                inbound,
            )

        services = list(services_by_name.values())

        # Match outbound calls to inbound APIs
        cross_edges, ambiguous = self._match_cross_edges(all_outbound, all_inbound)

        # Message channels are asynchronous service boundaries. Redis cache/data
        # access remains a resource dependency; Redis Pub/Sub becomes a message
        # edge only when a matching subscriber exists.
        message_edges, resource_edges = self._collect_non_http_dependencies()
        resource_graph: dict[str, list[str]] = defaultdict(list)
        for edge in resource_edges:
            if edge.resource_id not in resource_graph[edge.source_service]:
                resource_graph[edge.source_service].append(edge.resource_id)

        # Build dependency graph
        dep_graph: dict[str, list[str]] = defaultdict(list)
        for edge in cross_edges:
            if edge.target_service not in dep_graph[edge.source_service]:
                dep_graph[edge.source_service].append(edge.target_service)
        for edge in message_edges:
            if edge.target_service not in dep_graph[edge.source_service]:
                dep_graph[edge.source_service].append(edge.target_service)

        # Annotate services with dependencies
        for svc in services:
            svc.apis = all_inbound.get(svc.name, [])
            svc.dependencies = dep_graph.get(svc.name, [])
            svc.outbound_calls = [
                {
                    "source_endpoint_method": call.source_endpoint_method,
                    "source_endpoint_path": call.source_endpoint_path,
                    "source_endpoint_handler": call.source_endpoint_handler,
                    "caller_function": call.caller_function,
                    "caller_file": call.caller_file,
                    "caller_line": call.caller_line,
                    "http_method": call.http_method,
                    "url": call.url_or_pattern,
                    "call_chain": call.call_chain,
                }
                for call in all_outbound
                if call.caller_service == svc.name
            ]

        # Find potential unmatched edges
        potential = self._find_potential_edges(
            all_outbound, all_inbound, cross_edges, ambiguous
        )

        return UnifiedTopology(
            services=services,
            cross_edges=cross_edges,
            dependency_graph=dict(dep_graph),
            potential_edges=potential,
            message_edges=message_edges,
            resource_edges=resource_edges,
            resource_dependency_graph=dict(resource_graph),
        )

    def _collect_non_http_dependencies(
        self,
    ) -> tuple[list[MessageDependencyEdge], list[ResourceDependencyEdge]]:
        """Collect endpoint-rooted MQ/Redis edges through CodeGraph."""
        from .advanced_impl import AdvancedTopologyImplementation
        from .database import RedisDependencyCollector

        repositories = [
            {**member, "name": service["name"]}
            for service in self.repos
            for member in service.get("repositories", [service])
        ]
        collector = AdvancedTopologyImplementation(repositories)
        producers, consumers = collector._collect_mq_channels()
        message_edges: list[MessageDependencyEdge] = []
        emitted: set[tuple[str, str, str, str, str]] = set()
        for source_service, publications in producers.items():
            for publication in publications:
                channel = publication.get("topic", "")
                broker = publication["mq_type"]
                for target_service, subscriptions in consumers.items():
                    if target_service == source_service:
                        continue
                    for subscription in subscriptions:
                        if subscription["mq_type"] != broker or not self._message_channels_match(
                            channel, subscription.get("topic", "")
                        ):
                            continue
                        key = (
                            source_service,
                            publication.get("source_endpoint_handler", ""),
                            broker,
                            channel,
                            target_service,
                        )
                        if key in emitted:
                            continue
                        emitted.add(key)
                        message_edges.append(
                            MessageDependencyEdge(
                                source_service=source_service,
                                source_function=publication["function"],
                                target_service=target_service,
                                target_function=subscription["function"],
                                broker_type=broker,
                                channel=channel,
                                source_endpoint_method=publication.get(
                                    "source_endpoint_method", ""
                                ),
                                source_endpoint_path=publication.get("source_endpoint_path", ""),
                                source_endpoint_handler=publication.get(
                                    "source_endpoint_handler", ""
                                ),
                                source_entry_kind=publication.get("source_entry_kind", "http"),
                                call_chain=publication.get("call_chain", []),
                                confidence=0.9 if channel else 0.5,
                                evidence={
                                    "producer": publication.get("evidence", {}),
                                    "consumer": subscription.get("evidence", {}),
                                },
                            )
                        )

        resource_edges = RedisDependencyCollector(repositories).collect()
        return message_edges, resource_edges

    @staticmethod
    def _message_channels_match(produced: str, consumed: str) -> bool:
        """High-confidence channel matching for persisted dependencies."""
        if not produced or not consumed:
            return False
        left = produced.lower().strip().replace("-", "").replace("_", "")
        right = consumed.lower().strip().replace("-", "").replace("_", "")
        if "*" in left or "*" in right:
            pattern = re.escape(left).replace(r"\*", ".*")
            reverse = re.escape(right).replace(r"\*", ".*")
            return bool(re.fullmatch(pattern, right) or re.fullmatch(reverse, left))
        return left == right

    @staticmethod
    def _merge_service(services: dict[str, ServiceNode], incoming: ServiceNode) -> None:
        current = services.get(incoming.name)
        if current is None:
            services[incoming.name] = incoming
            return
        languages = [item for item in current.language.split("+") if item]
        if incoming.language not in languages:
            languages.append(incoming.language)
        current.language = "+".join(languages)
        if current.role != incoming.role and "frontend" in (current.role, incoming.role):
            current.role = "fullstack"
        for field_name in ("db_type", "mq_type"):
            values = [item for item in getattr(current, field_name).split("+") if item]
            incoming_value = getattr(incoming, field_name)
            if incoming_value and incoming_value not in values:
                values.append(incoming_value)
            setattr(current, field_name, "+".join(values))

    # ── Service analysis ────────────────────────────────────────────────

    def _analyze_service(self, name: str, path: str, db_path: str) -> ServiceNode:
        """Classify a service by its tech stack and role."""
        # Detect primary language
        with SQLiteCodeGraphRepository(db_path) as repository:
            language = repository.primary_language() or "unknown"

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
        defaults = {
            "postgres": ("postgres", "psycopg", "pg_", "postgresql"),
            "mysql": ("mysql", "mariadb"),
            "mongodb": ("mongo", "pymongo", "mongoose"),
            "redis": ("redis", "aioredis"),
            "sqlite": ("sqlite",),
        }
        db_patterns = self.rules.database_patterns or defaults
        for db_type, patterns in db_patterns.items():
            for p in patterns:
                with SQLiteCodeGraphRepository(db_path) as repository:
                    found = repository.has_node_name(p)
                if found:
                    return db_type
        return ""

    def _detect_mq(self, db_path: str) -> str:
        """Detect message queue type."""
        defaults = {
            "kafka": ("kafka",),
            "rabbitmq": ("rabbitmq", "amqp", "pika"),
            "nats": ("nats", "stan"),
            "redis_pubsub": ("redis.publish", "redis.subscribe"),
            "redis_streams": ("redis.xadd", "redis.xreadgroup"),
            "sqs": ("sqs",),
            "pubsub": ("pubsub",),
            "celery": ("celery",),
        }
        mq_patterns = self.rules.message_queue_patterns or defaults
        for mq, patterns in mq_patterns.items():
            for p in patterns:
                with SQLiteCodeGraphRepository(db_path) as repository:
                    found = repository.has_node_name(p)
                if found:
                    return mq
        return ""

    # ── Outbound HTTP call extraction ───────────────────────────────────

    def _extract_outbound_calls(
        self,
        service_name: str,
        db_path: str,
        inbound: list[dict],
        max_depth: int = 12,
    ) -> list[OutboundCall]:
        """Extract HTTP calls reachable from an inbound endpoint handler.

        CodeGraph's ``calls`` edges are the authority for reachability.  A
        client call that exists in the repository but cannot be reached from a
        resolved HTTP entry handler is deliberately omitted from the service
        dependency graph; it may belong to a job, test, or dead code.
        """
        with SQLiteCodeGraphRepository(db_path) as repository:
            language = repository.primary_language()
            patterns = self.rules.http_client_callers.get(language, [])
            if not patterns:
                return []

            # A single call can match more than one configured pattern. Keep
            # the first (most specific) rule and identify the call by graph
            # node IDs plus source line.
            calls: dict[tuple[str, str, int], tuple[dict, str | None]] = {}
            for pattern, method in patterns:
                for row in repository.http_client_calls(pattern):
                    key = (
                        row["caller_node_id"],
                        row["callee_node_id"],
                        int(row.get("call_line") or 0),
                    )
                    calls.setdefault(key, (row, method))
            if not calls:
                return []

            adjacency: dict[str, list[str]] = defaultdict(list)
            for edge in repository.call_edges():
                adjacency[edge["source"]].append(edge["target"])
            node_index = repository.call_node_index()

        calls_by_caller: dict[str, list[tuple[dict, str | None]]] = defaultdict(list)
        for row, method in calls.values():
            calls_by_caller[row["caller_node_id"]].append((row, method))

        results: list[OutboundCall] = []
        emitted: set[tuple[str, str, str, str, int]] = set()
        for endpoint in inbound:
            root = endpoint.get("node_id") or ""
            if not root:
                continue
            paths = self._shortest_paths_to_callers(
                root, adjacency, set(calls_by_caller), max_depth
            )
            for caller_id, path in paths.items():
                for row, method in calls_by_caller[caller_id]:
                    key = (
                        root,
                        endpoint.get("path") or "",
                        row["caller_node_id"],
                        row["callee_node_id"],
                        int(row.get("call_line") or 0),
                    )
                    if key in emitted:
                        continue
                    emitted.add(key)
                    url = self._extract_url_from_context(
                        db_path, row["caller_qname"], row["call_line"]
                    )
                    chain_ids = path + [row["callee_node_id"]]
                    chain = [
                        self._call_path_node(node_index, node_id)
                        for node_id in chain_ids
                    ]
                    results.append(
                        OutboundCall(
                            caller_service=service_name,
                            caller_function=row["caller_qname"],
                            caller_file=row["file_path"],
                            caller_line=row["caller_line"],
                            http_method=method,
                            url_or_pattern=url or "",
                            callee_name=row["callee_name"],
                            source_endpoint_method=endpoint.get("method") or "",
                            source_endpoint_path=endpoint.get("path") or "",
                            source_endpoint_handler=endpoint.get("handler") or "",
                            call_chain=chain,
                        )
                    )
        return results

    @staticmethod
    def _shortest_paths_to_callers(
        root: str,
        adjacency: dict[str, list[str]],
        targets: set[str],
        max_depth: int,
    ) -> dict[str, list[str]]:
        """Return a shortest CodeGraph call path from one endpoint to each target."""
        paths = {root: [root]}
        queue = deque([(root, 0)])
        found: dict[str, list[str]] = {}
        while queue:
            current, depth = queue.popleft()
            if current in targets:
                found[current] = paths[current]
            if depth >= max_depth:
                continue
            for callee in adjacency.get(current, []):
                if callee in paths:
                    continue
                paths[callee] = paths[current] + [callee]
                queue.append((callee, depth + 1))
        return found

    @staticmethod
    def _call_path_node(node_index: dict[str, dict], node_id: str) -> dict:
        node = node_index.get(node_id, {})
        return {
            "node_id": node_id,
            "name": node.get("name") or node_id,
            "qualified_name": node.get("qualified_name") or "",
            "kind": node.get("kind") or "",
            "file": node.get("file_path") or "",
            "line": int(node.get("start_line") or 0),
        }

    def _extract_url_from_context(
        self, db_path: str, caller_qname: str, call_line: int | None
    ) -> str | None:
        """Extract URL from near the HTTP call site (delegates to module helper)."""
        return extract_outbound_url(db_path, caller_qname, call_line)

    def _extract_url_from_source(self, file_path: str, call_line: int, db_path: str) -> str | None:
        """Read URL from source near the call line (delegates to module helper)."""
        return _url_from_source_lines(file_path, call_line, db_path)

    # ── Cross-service edge matching ─────────────────────────────────────

    def _match_cross_edges(
        self,
        outbound: list[OutboundCall],
        inbound: dict[str, list[dict]],
    ) -> tuple[list[CrossServiceEdge], list[dict]]:
        """Match outbound calls, retaining ambiguous endpoint candidates."""
        edges: list[CrossServiceEdge] = []
        ambiguous: list[dict] = []

        for call in outbound:
            url = call.url_or_pattern
            if not url:
                continue

            method = call.http_method

            # Extract path from URL
            path = self._extract_path(url)
            if not path:
                continue

            host = self._extract_host(url)
            host_target = self._known_service_for_host(host) if host else ""
            candidates: list[tuple[float, str, dict]] = []

            # An explicit host that is not a registered service is external.
            # Do not manufacture an internal dependency merely because its
            # path happens to resemble one of our APIs.
            if host and not host_target:
                continue

            # Try to match against each service's inbound APIs
            for svc_name, apis in inbound.items():
                if svc_name == call.caller_service:
                    continue  # skip self-calls
                if host_target and svc_name != host_target:
                    continue

                for api in apis:
                    api_method = api.get("method", "")
                    api_path = api.get("path", "")

                    # Method must match (or be unknown)
                    if method and api_method and method.upper() != api_method.upper():
                        continue

                    # Path matching
                    if self._paths_match(path, api_path):
                        exact_path = path.rstrip("/").lower() == api_path.rstrip("/").lower()
                        confidence = 1.0 if exact_path and method else 0.9 if method else 0.8
                        if host_target == svc_name:
                            confidence = min(1.0, confidence + 0.05)
                        candidates.append((confidence, svc_name, api))

            if not candidates:
                continue
            candidates = list(
                {
                    (
                        score,
                        service,
                        api.get("method", ""),
                        api.get("path", ""),
                        api.get("handler", ""),
                    ): (score, service, api)
                    for score, service, api in candidates
                }.values()
            )
            best_score = max(item[0] for item in candidates)
            best = [item for item in candidates if item[0] == best_score]
            if len(best) != 1:
                ambiguous.append(
                    self._ambiguous_dependency(call, path, host, best)
                )
                continue

            confidence, svc_name, api = best[0]
            api_method = api.get("method", "")
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
                    url_pattern=api.get("path", ""),
                    raw_url=url,
                    confidence=confidence,
                    evidence={
                        "entry_endpoint": (
                            f"{call.source_endpoint_method} {call.source_endpoint_path}"
                        ).strip(),
                        "caller": f"{call.caller_file}:{call.caller_line}",
                        "handler": f"{api.get('file', '')}:{api.get('line', 0)}",
                        "raw_url": url,
                        "normalized_path": path,
                        "host": host,
                        "call_chain": call.call_chain,
                    },
                    rule_version=self.rules.version,
                    source_endpoint_method=call.source_endpoint_method,
                    source_endpoint_path=call.source_endpoint_path,
                    source_endpoint_handler=call.source_endpoint_handler,
                    call_chain=call.call_chain,
                )
            )

        return edges, ambiguous

    def _ambiguous_dependency(
        self,
        call: OutboundCall,
        path: str,
        host: str,
        candidates: list[tuple[float, str, dict]],
    ) -> dict:
        return {
            "kind": "ambiguous",
            "source_service": call.caller_service,
            "source_endpoint_method": call.source_endpoint_method,
            "source_endpoint_path": call.source_endpoint_path,
            "source_endpoint_handler": call.source_endpoint_handler,
            "source_function": call.caller_function,
            "source_file": call.caller_file,
            "source_line": call.caller_line,
            "http_method": call.http_method,
            "url": call.url_or_pattern,
            "suspected_target": "",
            "reason": "Multiple registered endpoints matched with equal confidence",
            "confidence": candidates[0][0] if candidates else 0.0,
            "candidates": [
                {
                    "service": service,
                    "method": api.get("method", ""),
                    "path": api.get("path", ""),
                    "handler": api.get("handler", ""),
                    "confidence": score,
                }
                for score, service, api in candidates
            ],
            "evidence": {
                "host": host,
                "normalized_path": path,
                "call_chain": call.call_chain,
            },
            "match_rule": "ambiguous-method+path-template",
            "rule_version": self.rules.version,
        }

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
        ambiguous: list[dict] | None = None,
    ) -> list[dict]:
        """Find outbound calls that COULD be cross-service but didn't match.

        Useful for surfacing calls that might be to external services
        or where URL extraction failed to produce a matchable pattern.
        """
        del inbound  # retained for compatibility with older internal callers
        matched_callers = {
            (
                e.source_service,
                e.source_endpoint_path,
                e.source_function,
                e.raw_url,
            )
            for e in matched
        }

        potential = list(ambiguous or [])
        for call in outbound:
            key = (
                call.caller_service,
                call.source_endpoint_path,
                call.caller_function,
                call.url_or_pattern,
            )
            if key in matched_callers:
                continue

            if any(
                item.get("source_service") == call.caller_service
                and item.get("source_endpoint_path") == call.source_endpoint_path
                and item.get("source_function") == call.caller_function
                and item.get("url") == call.url_or_pattern
                for item in potential
            ):
                continue

            url = call.url_or_pattern
            if not url:
                potential.append(
                    {
                        "kind": "unresolved",
                        "source_service": call.caller_service,
                        "source_endpoint_method": call.source_endpoint_method,
                        "source_endpoint_path": call.source_endpoint_path,
                        "source_endpoint_handler": call.source_endpoint_handler,
                        "source_function": call.caller_function,
                        "source_file": call.caller_file,
                        "source_line": call.caller_line,
                        "http_method": call.http_method,
                        "url": "",
                        "suspected_target": "",
                        "reason": "HTTP client call is endpoint-reachable but its URL could not be resolved",
                        "confidence": 0.0,
                        "evidence": {"call_chain": call.call_chain},
                        "match_rule": "endpoint-reachability-only",
                        "rule_version": self.rules.version,
                    }
                )
                continue

            # Guess target service from URL hostname
            host = self._extract_host(url)
            if host:
                known_target = self._known_service_for_host(host)
                potential.append(
                    {
                        "kind": "internal_candidate" if known_target else "external",
                        "source_service": call.caller_service,
                        "source_endpoint_method": call.source_endpoint_method,
                        "source_endpoint_path": call.source_endpoint_path,
                        "source_endpoint_handler": call.source_endpoint_handler,
                        "source_function": call.caller_function,
                        "source_file": call.caller_file,
                        "source_line": call.caller_line,
                        "http_method": call.http_method,
                        "url": url,
                        "suspected_target": known_target or host,
                        "reason": "URL hostname identifies a registered service, but no endpoint matched"
                        if known_target
                        else "Possible external service",
                        "confidence": 0.35 if known_target else 0.1,
                        "evidence": {
                            "host": host,
                            "normalized_path": self._extract_path(url),
                            "call_chain": call.call_chain,
                        },
                        "match_rule": "hostname-heuristic",
                        "rule_version": self.rules.version,
                    }
                )
            else:
                potential.append(
                    {
                        "kind": "unresolved",
                        "source_service": call.caller_service,
                        "source_endpoint_method": call.source_endpoint_method,
                        "source_endpoint_path": call.source_endpoint_path,
                        "source_endpoint_handler": call.source_endpoint_handler,
                        "source_function": call.caller_function,
                        "source_file": call.caller_file,
                        "source_line": call.caller_line,
                        "http_method": call.http_method,
                        "url": url,
                        "suspected_target": "",
                        "reason": "Endpoint-reachable URL path did not match a registered endpoint",
                        "confidence": 0.0,
                        "evidence": {
                            "normalized_path": self._extract_path(url),
                            "call_chain": call.call_chain,
                        },
                        "match_rule": "unmatched-path",
                        "rule_version": self.rules.version,
                    }
                )

        return potential

    @staticmethod
    def _extract_host(url: str) -> str:
        """Extract hostname from a URL string."""
        m = re.search(r'://([^/\'",;?#]+)', url)
        return m.group(1) if m else ""

    def _known_service_for_host(self, host: str) -> str:
        """Resolve a URL host to a registered logical service without substring guesses."""
        host_name = host.rsplit("@", 1)[-1].split(":", 1)[0].lower().replace("_", "-")
        host_labels = {host_name, host_name.split(".", 1)[0]}
        matches = []
        for service in self.repos:
            aliases = [service.get("name", "")]
            aliases.extend(
                member.get("name", "")
                for member in service.get("repositories", [])
            )
            normalized = set()
            for alias in aliases:
                value = alias.lower().replace("_", "-")
                if not value:
                    continue
                normalized.add(value)
                if value.endswith("-service"):
                    normalized.add(value[: -len("-service")])
                elif value.endswith("-svc"):
                    normalized.add(value[: -len("-svc")])
                else:
                    normalized.update({f"{value}-service", f"{value}-svc"})
            if normalized & host_labels:
                matches.append(service.get("name", ""))
        return matches[0] if len(set(matches)) == 1 else ""

    # ── Impact analysis ─────────────────────────────────────────────────

    def impact_analysis(self, topology: UnifiedTopology, changed_service: str) -> dict:
        """Analyze which services are affected by a change to a given service.

        Returns upstream + downstream impact.
        """
        return ImpactAnalyzer().analyze(topology, changed_service)

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
        return FlowTracer().trace(topology, start_service, start_api_path or "", max_depth)

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
            f"Unified Topology: {len(t.services)} services, "
            f"{len(t.cross_edges)} HTTP edges, {len(t.message_edges)} message edges, "
            f"{len(t.resource_edges)} resource edges"
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
                source_endpoint = (
                    f"{e.source_endpoint_method} {e.source_endpoint_path}"
                    if e.source_endpoint_path
                    else e.source_function.split("::")[-1]
                )
                lines.append(
                    f"  {e.source_service}::{source_endpoint}"
                    f"  ──[{e.http_method} {e.url_pattern}]──→"
                    f"  {e.target_service}::{e.target_function.split('::')[-1]}"
                )
            if len(t.cross_edges) > 30:
                lines.append(f"  ... and {len(t.cross_edges) - 30} more")

        if t.message_edges:
            lines.append(f"\nMessage Edges ({len(t.message_edges)}):")
            for edge in t.message_edges[:30]:
                lines.append(
                    f"  {edge.source_service} ──[{edge.broker_type}:{edge.channel}]──→ "
                    f"{edge.target_service}::{edge.target_function.split('::')[-1]}"
                )

        if t.resource_edges:
            lines.append(f"\nResource Edges ({len(t.resource_edges)}):")
            for edge in t.resource_edges[:30]:
                key = f" key={edge.resource_key}" if edge.resource_key else ""
                lines.append(
                    f"  {edge.source_service} ──[{edge.operation}{key}]──→ {edge.resource_id}"
                )

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
