"""Configuration-file and consumer analysis."""

import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ...ports import SourceProvider

CONFIG_EXTENSIONS = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".properties", ".env", ".xml"}


class _Graph(Protocol):
    def file_records(self) -> list[dict[str, Any]]: ...
    def config_candidate_nodes(self) -> list[dict[str, Any]]: ...
    def get_function_by_id(self, node_id: str): ...


class ConfigUsageExtractor:
    def __init__(self, graph: _Graph | Callable[[], list[dict]], source: SourceProvider | None = None):
        self.graph, self.source = graph, source

    def extract(self) -> list[dict]:
        if callable(self.graph) and not hasattr(self.graph, "file_records"):
            return self.graph()
        files = self.graph.file_records()  # type: ignore[union-attr]
        paths = sorted({row["path"] for row in files if Path(row["path"]).suffix.lower() in CONFIG_EXTENSIONS})
        keys_by_file = {path: self.extract_keys(path) for path in paths}
        all_keys = {key.lower() for keys in keys_by_file.values() for key in keys}
        nodes = self.graph.config_candidate_nodes()  # type: ignore[union-attr]
        consumers: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            name = node["name"].lower()
            for key in all_keys:
                if key in name or name in key:
                    consumers[key].append(node["id"])
            for match in re.finditer(r"\$\{([^}]+)\}", str(node.get("decorators") or "")):
                consumers[match.group(1).lower()].append(node["id"])
        result = []
        for path, keys in keys_by_file.items():
            uses = []
            for key in keys:
                for node_id in consumers.get(key.lower(), [])[:5]:
                    function = self.graph.get_function_by_id(node_id)  # type: ignore[union-attr]
                    if function:
                        uses.append({"config_key": key, "consumer_name": function.qualified_name, "consumer_file": function.file_path, "consumer_line": function.start_line})
            if uses:
                result.append({"config_file": path, "key_count": len(keys), "consumed_keys": len({item["config_key"] for item in uses}), "consumers": uses[:50]})
        return result

    def extract_keys(self, path: str) -> list[str]:
        content = self.source.read_text(path) if self.source else None
        if content is None:
            return []
        extension = Path(path).suffix.lower()
        patterns = {
            ".yaml": r"^\s*([\w_-]+)\s*:", ".yml": r"^\s*([\w_-]+)\s*:",
            ".json": r'^\s*"([^"]+)"\s*:', ".env": r"^([A-Z_][A-Z0-9_]*)\s*=",
            ".properties": r"^([\w.\\-]+)\s*[=:]",
        }
        pattern = patterns.get(extension, r"^([\w_-]+)\s*[=:]")
        keys = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//", ";")):
                continue
            match = re.match(pattern, line)
            if match:
                keys.append(match.group(1).strip())
            if extension in {".toml", ".ini", ".cfg", ".conf"}:
                section = re.match(r"^\[([^]]+)\]", stripped)
                if section:
                    keys.append(section.group(1))
        return sorted(set(keys))
