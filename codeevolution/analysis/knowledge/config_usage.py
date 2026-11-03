"""Configuration-file and consumer analysis."""

import json
import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ...ports import SourceProvider

CONFIG_USAGE_VERSION = 2

CONFIG_EXTENSIONS = {
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".properties",
    ".env",
}

SOURCE_EXTENSIONS = {
    ".java",
    ".kt",
    ".kts",
    ".scala",
    ".groovy",
    ".py",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".vue",
    ".svelte",
    ".go",
    ".rb",
    ".php",
    ".cs",
    ".fs",
    ".rs",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".swift",
    ".lua",
    ".pl",
    ".pm",
    ".sh",
    ".bash",
}

# Real read sites only. "exact" keys must equal a declared config key;
# "prefix" keys bind every key under a namespace (@ConfigurationProperties).
_READ_PATTERNS = (
    (re.compile(r"""@Value\s*\(\s*["']\$\{([^}:]+)(?::[^}]*)?\}["']"""), "exact"),
    (
        re.compile(r"""@ConfigurationProperties\s*\(\s*(?:prefix\s*=\s*)?["']([^"']+)["']"""),
        "prefix",
    ),
    (
        re.compile(r"""@ConditionalOnProperty\s*\([^)]*?name\s*=\s*["']([^"']+)["']"""),
        "exact",
    ),
    (
        re.compile(
            r"""(?:environment|env|config|properties|settings)\.(?:getProperty|getRequiredProperty|get|getenv|get_env)\s*\(\s*["']([^"']+)["']"""
        ),
        "exact",
    ),
    (re.compile(r"""\bSystem\.(?:getenv|getProperty)\s*\(\s*["']([^"']+)["']"""), "exact"),
    (re.compile(r"""\bos\.getenv\s*\(\s*["']([^"']+)["']"""), "exact"),
    (re.compile(r"""\bos\.environ(?:\.get)?\s*[\[(]\s*["']([^"']+)["']"""), "exact"),
    (re.compile(r"""\bprocess\.env\.([A-Za-z_]\w*)"""), "exact"),
    (re.compile(r"""\bprocess\.env\s*\[\s*["']([^"']+)["']\s*\]"""), "exact"),
    (re.compile(r"""\bimport\.meta\.env\.([A-Za-z_]\w*)"""), "exact"),
    (re.compile(r"""\bos\.(?:Getenv|LookupEnv)\s*\(\s*["']([^"']+)["']"""), "exact"),
    (
        re.compile(
            r"""\bviper\.(?:Get|GetString|GetInt|GetBool|GetDuration|GetFloat64)\w*\s*\(\s*["']([^"']+)["']"""
        ),
        "exact",
    ),
    (re.compile(r"""\bENV\s*\[\s*["']([^"']+)["']\s*\]"""), "exact"),
    (re.compile(r"""\bgetenv\s*\(\s*["']([^"']+)["']"""), "exact"),
    (re.compile(r"""\bGetEnvironmentVariable\s*\(\s*["']([^"']+)["']"""), "exact"),
    (re.compile(r"""\bConfiguration\s*\[\s*["']([^"']+)["']\s*\]"""), "exact"),
    (re.compile(r"""\b(?:settings|config)\.([A-Za-z_]\w*)"""), "exact"),
)

_SCAN_MARKERS = (
    "@Value",
    "@ConfigurationProperties",
    "@ConditionalOnProperty",
    "getenv",
    "Getenv",
    "environ",
    "LookupEnv",
    "process.env",
    "import.meta.env",
    "getProperty",
    "getRequiredProperty",
    "getString",
    "viper.",
    "ENV[",
    "GetEnvironmentVariable",
    "Configuration[",
    "settings.",
    "config.",
)


class _Graph(Protocol):
    def file_records(self) -> list[dict[str, Any]]: ...
    def config_candidate_nodes(self) -> list[dict[str, Any]]: ...


class ConfigUsageExtractor:
    def __init__(
        self, graph: _Graph | Callable[[], list[dict]], source: SourceProvider | None = None
    ):
        self.graph, self.source = graph, source

    def extract(self) -> list[dict]:
        if callable(self.graph) and not hasattr(self.graph, "file_records"):
            return self.graph()
        files = self.graph.file_records()  # type: ignore[union-attr]
        paths = sorted(
            {row["path"] for row in files if Path(row["path"]).suffix.lower() in CONFIG_EXTENSIONS}
        )
        keys_by_file = {path: self.extract_keys(path) for path in paths}
        keys_by_file = {path: keys for path, keys in keys_by_file.items() if keys}

        index: dict[str, dict] = {}
        for path, keys in keys_by_file.items():
            for key in keys:
                entry = index.setdefault(key.lower(), {"key": key, "files": set()})
                entry["files"].add(path)

        consumers: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        seen: set[tuple[str, str, int]] = set()
        for site in self._read_sites(files):
            if site["mode"] == "prefix":
                matched = [
                    lowered
                    for lowered in index
                    if lowered == site["key"] or lowered.startswith(site["key"] + ".")
                ]
            else:
                matched = [site["key"]] if site["key"] in index else []
            for lowered in matched:
                entry = index[lowered]
                for path in entry["files"]:
                    ident = (entry["key"], site["file"], site["line"])
                    if ident in seen:
                        continue
                    seen.add(ident)
                    consumers[path][entry["key"]].append(
                        {
                            "config_key": entry["key"],
                            "consumer_name": site["consumer"],
                            "consumer_file": site["file"],
                            "consumer_line": site["line"],
                        }
                    )

        result = []
        for path, keys in keys_by_file.items():
            uses = [item for key in keys for item in consumers.get(path, {}).get(key, [])]
            result.append(
                {
                    "config_file": path,
                    "key_count": len(keys),
                    "consumed_keys": len({item["config_key"] for item in uses}),
                    "consumers": uses[:50],
                }
            )
        return result

    def extract_keys(self, path: str) -> list[str]:
        content = self.source.read_text(path) if self.source else None
        if content is None:
            return []
        extension = Path(path).suffix.lower()
        if extension in {".yaml", ".yml"}:
            keys = _yaml_keys(content)
        elif extension == ".json":
            keys = _json_keys(content)
        elif extension in {".toml", ".ini", ".cfg", ".conf"}:
            keys = _sectioned_keys(content)
        elif extension == ".properties":
            keys = _flat_keys(content, r"^([\w.\\-]+)\s*[=:]")
        elif extension == ".env":
            keys = _flat_keys(content, r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")
        else:
            keys = _flat_keys(content, r"^([\w.\\-]+)\s*[=:]")
        return sorted({key for key in keys if key})

    def _read_sites(self, files: list[dict]) -> list[dict]:
        if self.source is None:
            return []
        ranges = self._function_ranges()
        sites = []
        for row in files:
            path = row["path"]
            if Path(path).suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            text = self.source.read_text(path)
            if not text:
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if not any(marker in line for marker in _SCAN_MARKERS):
                    continue
                for pattern, mode in _READ_PATTERNS:
                    for match in pattern.finditer(line):
                        key = match.group(1).strip().lower()
                        if not key:
                            continue
                        sites.append(
                            {
                                "key": key,
                                "mode": mode,
                                "file": path,
                                "line": line_number,
                                "consumer": self._consumer_name(
                                    path, line_number, line.strip(), ranges
                                ),
                            }
                        )
        return sites

    def _function_ranges(self) -> dict[str, list[tuple[int, int, str]]]:
        ranges: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        for node in self.graph.config_candidate_nodes():  # type: ignore[union-attr]
            if node.get("kind") not in {"function", "method"}:
                continue
            start = int(node.get("start_line") or 0)
            end = int(node.get("end_line") or start)
            if start:
                ranges[node["file_path"]].append(
                    (start, end, node.get("qualified_name") or node.get("name") or "")
                )
        return ranges

    @staticmethod
    def _consumer_name(
        path: str, line_number: int, text: str, ranges: dict[str, list[tuple[int, int, str]]]
    ) -> str:
        best: tuple[int, int, str] | None = None
        for start, end, name in ranges.get(path, ()):
            if start <= line_number <= end and (best is None or end - start < best[1] - best[0]):
                best = (start, end, name)
        return best[2] if best else text[:120]


def _flat_keys(content: str, pattern: str) -> list[str]:
    keys = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!", "//", ";")):
            continue
        match = re.match(pattern, stripped)
        if match:
            keys.append(match.group(1).strip())
    return keys


def _sectioned_keys(content: str) -> list[str]:
    keys, section = [], ""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        heading = re.match(r"^\[([^\]]+)\]", stripped)
        if heading:
            section = heading.group(1).strip()
            continue
        match = re.match(r"^([\w.-]+)\s*[=:]", stripped)
        if match:
            key = match.group(1)
            keys.append(f"{section}.{key}" if section else key)
    return keys


def _yaml_keys(content: str) -> list[str]:
    """Dotted key paths from block-style YAML (no dependency)."""
    keys: list[str] = []
    stack: list[tuple[int, str]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "---", "- ")):
            continue
        match = re.match(r"^(\s*)([^:#][^:]*?)\s*:(?:\s|$)", line)
        if not match:
            continue
        indent = len(match.group(1).expandtabs(2))
        key = match.group(2).strip().strip("'\"")
        if not key:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        keys.append(".".join(item[1] for item in stack))
    return keys


def _json_keys(content: str) -> list[str]:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return _flat_keys(content, r'^\s*"([^"]+)"\s*:')
    keys: list[str] = []

    def walk(prefix: str, value) -> None:
        if isinstance(value, dict):
            for name, child in value.items():
                walk(f"{prefix}.{name}" if prefix else str(name), child)
        elif prefix:
            keys.append(prefix)

    walk("", data)
    return keys
