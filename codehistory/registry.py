"""Multi-repo registry — manages registered code repositories with auto-detection.

At registration time, each repo is scanned via CodeGraph SQLite to detect:
  - Primary language (from files table)
  - Service role (gateway/backend/worker/cron, from directory naming)
  - Database type (postgres/mysql/mongodb/redis, from imports/decorators)
  - Message queue type (kafka/rabbitmq/nats, from imports)
  - CodeGraph status (initialized, index freshness, symbol/edge counts)
"""

import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


REGISTRY_DIR = Path.home() / ".codehistory"
REGISTRY_FILE = REGISTRY_DIR / "registry.json"
TOPOLOGY_CACHE_FILE = REGISTRY_DIR / "topology_cache.json"


# ── Service role inference ─────────────────────────────────────────────

ROLE_PATTERNS = [
    (("gateway", "proxy", "bff", "ingress", "nginx", "apigateway", "api-gateway"), "gateway"),
    (("worker", "consumer", "processor", "handler", "executor"), "worker"),
    (("cron", "scheduler", "timer", "job", "trigger"), "cron"),
    (("frontend", "web", "ui", "dashboard", "console", "app"), "frontend"),
    (("mobile", "ios", "android", "flutter", "react-native"), "mobile"),
]

DB_PATTERNS = {
    "postgresql": ("postgres", "psycopg", "pg_", "postgresql", "pgx"),
    "mysql": ("mysql", "mariadb", "mysqli"),
    "mongodb": ("mongo", "pymongo", "mongoose", "mongoc"),
    "redis": ("redis", "aioredis", "redigo", "go-redis"),
    "sqlite": ("sqlite", "better-sqlite3"),
    "elasticsearch": ("elasticsearch", "opensearch"),
}

MQ_PATTERNS = {
    "kafka": ("kafka", "confluent_kafka", "sarama", "kafka-go"),
    "rabbitmq": ("rabbitmq", "amqp", "pika", "amqplib"),
    "nats": ("nats", "stan", "nats.go", "nats-server"),
    "sqs": ("sqs", "aws-sqs", "amazon-sqs"),
    "pubsub": ("pubsub", "google-pubsub", "cloud-pubsub"),
    "celery": ("celery",),
    "pulsar": ("pulsar",),
}

CACHE_PATTERNS = {
    "redis": ("redis", "aioredis", "redigo"),
    "memcached": ("memcache", "memcached", "memcache-client"),
}


# ── Data types ─────────────────────────────────────────────────────────

@dataclass
class ServiceMeta:
    """Auto-detected metadata for a registered service."""
    name: str
    path: str
    language: str = ""
    role: str = ""
    db_types: list[str] = field(default_factory=list)
    mq_types: list[str] = field(default_factory=list)
    cache_types: list[str] = field(default_factory=list)
    cg_initialized: bool = False
    cg_nodes: int = 0
    cg_edges: int = 0
    cg_files: int = 0
    cg_age_hours: float = 0.0       # hours since last index
    cg_stale: bool = False           # modified files exist since last index
    git_remotes: list[str] = field(default_factory=list)
    registered_at: str = ""


# ── Registry CRUD ──────────────────────────────────────────────────────

def ensure_registry():
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    if not REGISTRY_FILE.exists():
        REGISTRY_FILE.write_text("[]")


def load_registry() -> list[dict]:
    ensure_registry()
    try:
        data = json.loads(REGISTRY_FILE.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def save_registry(entries: list[dict]):
    ensure_registry()
    REGISTRY_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False))


def register_repo(name: str, path: str) -> dict:
    """Register a new repo with auto-detected metadata.

    Scans the repo's CodeGraph database (if available) and git remotes
    to populate service metadata at registration time.
    """
    abs_path = str(Path(path).resolve())
    if not Path(abs_path, ".git").exists():
        raise ValueError(f"Not a git repository: {abs_path}")

    entries = load_registry()

    for e in entries:
        if e["name"] == name:
            raise ValueError(f"Repo name '{name}' already registered")
        if e["path"] == abs_path:
            raise ValueError(f"Repo path already registered as '{e['name']}'")

    meta = detect_service(abs_path)

    from datetime import datetime, timezone
    entry = {
        "name": name,
        "path": abs_path,
        "language": meta.language,
        "role": meta.role,
        "db_types": meta.db_types,
        "mq_types": meta.mq_types,
        "cache_types": meta.cache_types,
        "cg_initialized": meta.cg_initialized,
        "git_remotes": meta.git_remotes,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    entries.append(entry)
    save_registry(entries)
    return entry


def refresh_meta(name: str) -> dict | None:
    """Re-scan a registered service and update its metadata."""
    entries = load_registry()
    for e in entries:
        if e["name"] == name:
            meta = detect_service(e["path"])
            from datetime import datetime, timezone
            e["language"] = meta.language
            e["role"] = meta.role
            e["db_types"] = meta.db_types
            e["mq_types"] = meta.mq_types
            e["cache_types"] = meta.cache_types
            e["cg_initialized"] = meta.cg_initialized
            e["git_remotes"] = meta.git_remotes
            e["registered_at"] = datetime.now(timezone.utc).isoformat()
            save_registry(entries)
            return e
    return None


def unregister_repo(name: str):
    entries = load_registry()
    entries = [e for e in entries if e["name"] != name]
    save_registry(entries)


def get_repo(name: str) -> dict | None:
    for e in load_registry():
        if e["name"] == name:
            return e
    return None


def list_repos() -> list[dict]:
    return load_registry()


# ── Service detection ──────────────────────────────────────────────────

def detect_service(repo_path: str) -> ServiceMeta:
    """Scan a repo and auto-detect its metadata.

    Reads CodeGraph SQLite (if available) for language/stack analysis.
    Also reads git remotes for service identity hints.
    """
    meta = ServiceMeta(
        name=Path(repo_path).name,
        path=repo_path,
    )

    cg_db = Path(repo_path) / ".codegraph" / "codegraph.db"
    meta.cg_initialized = cg_db.exists()

    if meta.cg_initialized:
        _detect_from_codegraph(meta, str(cg_db))
    else:
        # Fallback: detect language from file extensions
        _detect_from_filesystem(meta, repo_path)

    # Infer role from directory name
    meta.role = _infer_role(repo_path)

    # Git remotes
    meta.git_remotes = _get_git_remotes(repo_path)

    return meta


def _detect_from_codegraph(meta: ServiceMeta, db_path: str):
    """Detect metadata from CodeGraph's SQLite database."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return

    try:
        # Language
        row = conn.execute("""
            SELECT language, COUNT(*) AS cnt FROM files
            WHERE language != 'unknown'
            GROUP BY language ORDER BY cnt DESC LIMIT 1
        """).fetchone()
        if row:
            meta.language = row["language"]

        # DB/MQ/Cache detection — scan for known library imports
        imports = conn.execute("""
            SELECT name, signature FROM nodes WHERE kind = 'import'
        """).fetchall()
        all_names = " ".join(
            (r["signature"] or r["name"]).lower() for r in imports
        )

        for db, patterns in DB_PATTERNS.items():
            if any(p in all_names for p in patterns):
                meta.db_types.append(db)

        for mq, patterns in MQ_PATTERNS.items():
            if any(p in all_names for p in patterns):
                meta.mq_types.append(mq)

        for cache, patterns in CACHE_PATTERNS.items():
            if any(p in all_names for p in patterns):
                meta.cache_types.append(cache)

        # Size stats
        nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()
        edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()
        meta.cg_nodes = nodes[0] if nodes else 0
        meta.cg_edges = edges[0] if edges else 0
        meta.cg_files = files[0] if files else 0

        # Index freshness: age in hours
        row = conn.execute("SELECT MAX(indexed_at) FROM files").fetchone()
        if row and row[0]:
            import time
            age_sec = time.time() - row[0] / 1000
            meta.cg_age_hours = round(age_sec / 3600, 1)

        # Staleness: files modified after last index
        stale = conn.execute("""
            SELECT COUNT(*) FROM files WHERE modified_at > indexed_at
        """).fetchone()
        meta.cg_stale = (stale[0] or 0) > 0 if stale else False
    finally:
        conn.close()


def _detect_from_filesystem(meta: ServiceMeta, repo_path: str):
    """Fallback language detection by counting file extensions (no CodeGraph)."""
    counts: dict[str, int] = defaultdict(int)
    ext_map = {
        ".py": "python", ".java": "java", ".go": "go",
        ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
        ".jsx": "javascript", ".rs": "rust", ".rb": "ruby",
        ".php": "php", ".cs": "csharp", ".swift": "swift",
        ".kt": "kotlin", ".dart": "dart", ".cpp": "cpp", ".c": "c",
        ".vue": "vue", ".svelte": "svelte",
    }
    ignore_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv",
                   "dist", "build", "target", ".next", ".codegraph",
                   ".codehistory"}
    try:
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
            for f in files:
                ext = Path(f).suffix.lower()
                if ext in ext_map:
                    counts[ext_map[ext]] += 1
            if len(counts) > 10:
                break  # sample enough
    except OSError:
        pass

    if counts:
        meta.language = max(counts, key=counts.get)


def _infer_role(repo_path: str) -> str:
    """Infer service role from directory/repo naming conventions."""
    name_lower = Path(repo_path).name.lower()

    for keywords, role in ROLE_PATTERNS:
        if any(k in name_lower for k in keywords):
            return role

    # Check parent directory context
    parent = Path(repo_path).parent.name.lower()
    if "gateway" in parent:
        return "gateway"
    if "worker" in parent:
        return "worker"
    if "frontend" in parent or "web" in parent:
        return "frontend"

    return "backend"


def _get_git_remotes(repo_path: str) -> list[str]:
    """Get git remote URLs for a repo."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "remote", "-v"],
            capture_output=True, text=True, timeout=5,
        )
        remotes = set()
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split()
                if len(parts) >= 2:
                    remotes.add(parts[1])
        return sorted(remotes)
    except Exception:
        return []


# ── Discovery ──────────────────────────────────────────────────────────

def discover_repos(root_dir: str, max_depth: int = 2) -> list[dict]:
    """Scan a directory tree for git repositories and suggest registrations.

    Args:
        root_dir: Root directory to scan.
        max_depth: Maximum directory depth to search for .git directories.

    Returns:
        List of {name, path, language, role, suggestion} for each found repo.
    """
    root = Path(root_dir).resolve()
    results: list[dict] = []

    # Use find to locate .git directories
    import subprocess
    try:
        result = subprocess.run(
            ["find", str(root), "-maxdepth", str(max_depth + 1),
             "-name", ".git", "-type", "d", "-not", "-path", "*/node_modules/*"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return results

    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        git_dir = Path(line)
        repo_path = str(git_dir.parent)

        # Skip if already registered
        existing = list_repos()
        if any(e["path"] == repo_path for e in existing):
            continue

        # Quick scan
        meta = ServiceMeta(name=repo_path.split("/")[-1], path=repo_path)
        cg_db = git_dir.parent / ".codegraph" / "codegraph.db"
        if cg_db.exists():
            _detect_from_codegraph(meta, str(cg_db))
        else:
            _detect_from_filesystem(meta, repo_path)
        meta.role = _infer_role(repo_path)

        suggestion = ""
        if meta.cg_initialized:
            suggestion = f"Ready — CodeGraph indexed ({meta.cg_nodes} symbols)"
        else:
            suggestion = "Run: codegraph init"

        results.append({
            "name": git_dir.parent.name,
            "path": repo_path,
            "language": meta.language,
            "role": meta.role,
            "cg_initialized": meta.cg_initialized,
            "cg_nodes": meta.cg_nodes,
            "suggestion": suggestion,
        })

    return results


# ── Health check ───────────────────────────────────────────────────────

def check_services() -> list[dict]:
    """Check health of all registered services.

    Returns one entry per service with status and any issues found.
    """
    results = []
    entries = load_registry()

    for e in entries:
        status = "ok"
        issues = []
        meta = detect_service(e["path"])

        if not Path(e["path"]).exists():
            status = "error"
            issues.append("Repository directory not found")
        elif not Path(e["path"], ".git").exists():
            status = "error"
            issues.append("Not a git repository (.git missing)")
        elif not meta.cg_initialized:
            status = "warning"
            issues.append("CodeGraph not initialized — run: codegraph init")
        elif meta.cg_stale:
            status = "warning"
            issues.append(f"Index stale ({meta.cg_age_hours}h old) — run: codegraph sync")
        elif meta.cg_age_hours > 24:
            status = "info"
            issues.append(f"Index {meta.cg_age_hours}h old — consider running: codegraph sync")

        results.append({
            "name": e["name"],
            "path": e["path"],
            "status": status,
            "language": meta.language,
            "role": meta.role,
            "cg_initialized": meta.cg_initialized,
            "cg_symbols": meta.cg_nodes,
            "cg_edges": meta.cg_edges,
            "cg_age_hours": meta.cg_age_hours,
            "db_types": meta.db_types,
            "mq_types": meta.mq_types,
            "issues": issues,
        })

    return results


# ── Topology cache ─────────────────────────────────────────────────────

def load_topology_cache() -> dict | None:
    """Load the cached unified topology from the last full analysis."""
    if not TOPOLOGY_CACHE_FILE.exists():
        return None
    try:
        return json.loads(TOPOLOGY_CACHE_FILE.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return None


def save_topology_cache(data: dict):
    """Cache the unified topology for instant impact/trace queries."""
    TOPOLOGY_CACHE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def is_topology_cache_stale(entries: list[dict]) -> bool:
    """Check if the topology cache needs rebuilding.

    Returns True if any service's CodeGraph DB is newer than the cache.
    """
    cached = load_topology_cache()
    if not cached:
        return True

    cache_time = cached.get("_built_at", 0)
    for e in entries:
        cg_db = Path(e["path"]) / ".codegraph" / "codegraph.db"
        if cg_db.exists():
            mtime = cg_db.stat().st_mtime
            if mtime > cache_time:
                return True
    return False


def build_topology_cache() -> dict | None:
    """Build and cache the unified topology. Returns None on failure."""
    from .cross_repo import CrossRepoAnalyzer

    entries = list_repos()
    if not entries:
        return None

    analyzer = CrossRepoAnalyzer(entries)
    topology = analyzer.analyze()

    cache_data = {
        "_built_at": __import__("time").time(),
        "_service_count": len(topology.services),
        "_edge_count": len(topology.cross_edges),
        "services": [
            {
                "name": s.name,
                "path": s.repo_path,
                "language": s.language,
                "role": s.role,
                "db_types": [s.db_type] if s.db_type else [],
                "mq_types": [s.mq_type] if s.mq_type else [],
                "api_count": len(s.apis),
                "dependencies": s.dependencies,
            }
            for s in topology.services
        ],
        "dependency_graph": topology.dependency_graph,
        "cross_edges": [
            {
                "source_service": e.source_service,
                "source_function": e.source_function,
                "target_service": e.target_service,
                "target_function": e.target_function,
                "http_method": e.http_method,
                "url_pattern": e.url_pattern,
            }
            for e in topology.cross_edges
        ],
        "potential_edges": topology.potential_edges,
    }

    save_topology_cache(cache_data)
    return cache_data


def get_cached_impact(service_name: str) -> dict | None:
    """Get instant impact analysis from cached topology."""
    cached = load_topology_cache()
    if not cached:
        return None

    dep_graph = cached.get("dependency_graph", {})
    edges = cached.get("cross_edges", [])

    downstream = dep_graph.get(service_name, [])
    upstream = [svc for svc, deps in dep_graph.items() if service_name in deps]
    affected = [e for e in edges
                if e["source_service"] == service_name
                or e["target_service"] == service_name]

    return {
        "service": service_name,
        "_from_cache": True,
        "upstream_impact": upstream,
        "downstream_impact": downstream,
        "affected_cross_edges": affected,
    }


def get_cached_trace(service_name: str, api_path: str | None = None, max_depth: int = 5) -> list[dict] | None:
    """Get end-to-end trace from cached topology."""
    cached = load_topology_cache()
    if not cached:
        return None

    edges = cached.get("cross_edges", [])
    chain: list[dict] = []
    visited: set[tuple[str, str, str]] = set()

    def follow(svc: str, depth: int):
        if depth > max_depth:
            return
        for e in edges:
            if e["source_service"] != svc:
                continue
            if api_path and e["url_pattern"] != api_path:
                continue
            key = (e["source_service"], e["target_service"], e["url_pattern"])
            if key in visited:
                continue
            visited.add(key)
            chain.append({**e, "depth": depth})
            follow(e["target_service"], depth + 1)

    follow(service_name, 0)
    return chain
