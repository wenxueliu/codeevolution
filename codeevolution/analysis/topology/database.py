"""Static database and Redis resource dependency collectors."""

import re
from pathlib import Path

from ...infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository

DB_CALL_PATTERNS = (
    "execute",
    "executemany",
    "query",
    "select",
    "insert",
    "update",
    "delete",
    "save",
    "find",
    "findone",
    "findall",
    "repository",
    "cursor",
)

REDIS_OPERATIONS = {
    "get", "set", "mget", "mset", "delete", "del", "hget", "hset",
    "lpush", "rpush", "lpop", "rpop", "sadd", "srem", "zadd", "zrem",
    "incr", "decr", "expire", "setex", "scan", "keys", "publish",
    "subscribe", "psubscribe", "xadd", "xread", "xreadgroup",
}


class DatabaseAccessCollector:
    def __init__(self, repos: list[dict]):
        self.repos = repos

    def collect(self) -> dict[str, list[dict]]:
        result = {}
        for repo in self.repos:
            database = Path(repo["path"]) / ".codegraph" / "codegraph.db"
            if not database.exists():
                result[repo["name"]] = []
                continue
            with SQLiteCodeGraphRepository(str(database)) as repository:
                rows = repository.database_call_candidates()
            accesses = []
            for row in rows:
                searchable = " ".join(
                    str(row.get(key) or "") for key in ("target", "name", "signature", "metadata")
                )
                if not any(pattern in searchable.lower() for pattern in DB_CALL_PATTERNS):
                    continue
                accesses.append(
                    {
                        "function": row["function"],
                        "target": row["target"],
                        "table": self.extract_table(searchable),
                        "evidence": {"caller": row["function"], "callee": row["target"]},
                    }
                )
            result[repo["name"]] = accesses
        return result

    @staticmethod
    def extract_table(text: str) -> str:
        match = re.search(r"\b(?:from|into|update|join|table)\s+([\w.\"`]+)", text, re.I)
        return match.group(1).strip('"`') if match else "unknown"


class RedisDependencyCollector:
    """Build endpoint-rooted service-to-Redis resource edges."""

    def __init__(self, repos: list[dict]):
        self.repos = repos
        self._instance_cache: dict[Path, str] = {}

    def collect(self) -> list:
        from .advanced_impl import AdvancedTopologyImplementation
        from .cross_repo_impl import ResourceDependencyEdge

        result = []
        emitted: set[tuple[str, str, str, str, int]] = set()
        mq_collector = AdvancedTopologyImplementation(self.repos)
        _, all_consumers = mq_collector._collect_mq_channels()
        for repo in self.repos:
            database = Path(repo["path"]) / ".codegraph" / "codegraph.db"
            if not database.exists():
                continue
            with SQLiteCodeGraphRepository(str(database)) as repository:
                if not (
                    repository.has_node_name("redis")
                    or repository.has_node_name("aioredis")
                ):
                    continue
                rows = repository.database_call_candidates()
                adjacency: dict[str, list[str]] = {}
                for edge in repository.call_edges():
                    adjacency.setdefault(edge["source"], []).append(edge["target"])
                node_index = repository.call_node_index()

            roots = mq_collector._service_entry_roots(
                str(database), all_consumers.get(repo["name"], [])
            )
            for row in rows:
                source = self._source_context(database, row)
                operation = self._operation(row, source)
                if not operation:
                    continue
                contexts = mq_collector._reachable_entry_contexts(
                    roots, row["caller_node_id"], adjacency
                )
                for root, path in contexts:
                    key = (
                        repo["name"], root["node_id"], row["caller_node_id"],
                        operation, int(row.get("call_line") or 0),
                    )
                    if key in emitted:
                        continue
                    emitted.add(key)
                    chain = [
                        mq_collector._call_path_node(node_index, node_id)
                        for node_id in path + [row["callee_node_id"]]
                    ]
                    result.append(
                        ResourceDependencyEdge(
                            source_service=repo["name"],
                            source_function=row["function"],
                            resource_type="redis",
                            resource_id=self._instance_id(database.parent.parent, source),
                            operation=operation.upper(),
                            resource_key=self._resource_key(source),
                            source_endpoint_method=root.get("method", ""),
                            source_endpoint_path=root.get("path", ""),
                            source_endpoint_handler=root["handler"],
                            source_entry_kind=root["kind"],
                            call_chain=chain,
                            confidence=0.9 if "redis" in str(row["target"]).lower() else 0.75,
                            evidence={
                                "caller": f"{row.get('file_path', '')}:{row.get('call_line') or row.get('start_line', 0)}",
                                "callee": row["target"],
                            },
                        )
                    )
        return result

    @staticmethod
    def _operation(row: dict, source: str) -> str:
        target = f"{row.get('target', '')}.{row.get('name', '')}".lower()
        if "redis" not in target and not re.search(
            r"\b(?:redis|cache)\w*\.\w+\s*\(", source.splitlines()[0] if source else "", re.I
        ):
            return ""
        tokens = set(re.findall(r"[a-z][a-z0-9_]*", target))
        matches = REDIS_OPERATIONS & tokens
        return sorted(matches, key=len, reverse=True)[0] if matches else ""

    @staticmethod
    def _source_context(database: Path, row: dict) -> str:
        source_path = database.parent.parent / str(row.get("file_path") or "")
        try:
            lines = source_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return ""
        line = int(row.get("call_line") or row.get("start_line") or 0)
        call_text = lines[line - 1] if 0 < line <= len(lines) else ""
        return "\n".join([call_text, *lines[:100]])

    def _instance_id(self, repo_root: Path, source: str) -> str:
        match = re.search(r"redis(?:s)?://[^\s'\"),]+", source, re.I)
        if match:
            return re.sub(r"(redis(?:s)?://)[^/@]+@", r"\1", match.group(0), flags=re.I)
        host = re.search(r"(?:host|REDIS_HOST)\s*=\s*['\"]([^'\"]+)", source, re.I)
        port = re.search(r"(?:port|REDIS_PORT)\s*=\s*(\d+)", source, re.I)
        if host:
            return f"redis://{host.group(1)}:{port.group(1) if port else '6379'}"
        if repo_root in self._instance_cache:
            return self._instance_cache[repo_root]
        config_text = ""
        for pattern in ("application*.yml", "application*.yaml", "application*.properties", ".env*"):
            for path in repo_root.rglob(pattern):
                try:
                    if path.stat().st_size <= 1_000_000:
                        config_text += "\n" + path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
        url = re.search(r"redis(?:s)?://[^\s'\"),]+", config_text, re.I)
        if url:
            result = re.sub(
                r"(redis(?:s)?://)[^/@]+@", r"\1", url.group(0), flags=re.I
            )
            self._instance_cache[repo_root] = result
            return result
        host = re.search(
            r"(?:spring[._-])?redis[._-]host\s*[:=]\s*['\"]?([^\s'\"#]+)",
            config_text,
            re.I,
        )
        if not host:
            host = re.search(
                r"\bredis\s*:\s*(?:\n\s+[^\n]+)*?\n\s+host\s*:\s*['\"]?([^\s'\"#]+)",
                config_text,
                re.I,
            )
        port = re.search(
            r"(?:spring[._-])?redis[._-]port\s*[:=]\s*['\"]?(\d+)", config_text, re.I
        )
        if host:
            result = f"redis://{host.group(1)}:{port.group(1) if port else '6379'}"
            self._instance_cache[repo_root] = result
            return result
        self._instance_cache[repo_root] = "redis:default"
        return "redis:default"

    @staticmethod
    def _resource_key(source: str) -> str:
        call_text = source.splitlines()[0] if source else ""
        literal = re.search(r"\(\s*['\"]([^'\"]+)['\"]", call_text)
        if literal:
            return literal.group(1)
        expression = re.search(r"\(\s*([^,)]+)", call_text)
        return expression.group(1).strip()[:80] if expression else "unknown"
