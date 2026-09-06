"""Frontend HTTP-call discovery and backend endpoint matching."""

from __future__ import annotations

import re
from pathlib import Path

HTTP_BLOCK = re.compile(
    r"(?:export\s+)?(?:async\s+)?function\s+(?P<name>[\w$]+)[^{]*\{.*?"
    r"(?:return\s+)?(?:http|request|axios)(?:<[^;{}]+>)?\s*\(\s*\{(?P<config>.*?)\}\s*\)",
    re.DOTALL,
)


def extract_frontend_calls(repo_path: str) -> list[dict]:
    root = Path(repo_path)
    source_root = root / "src"
    if not source_root.exists():
        return []
    calls = []
    source_files = [
        path
        for pattern in ("*.js", "*.ts", "*.jsx", "*.tsx", "*.vue")
        for path in source_root.rglob(pattern)
    ]
    contents = {}
    for path in source_files:
        try:
            contents[path] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

    for path, content in contents.items():
        if "/apis/" not in path.as_posix() and "/api/" not in path.as_posix():
            continue
        for match in HTTP_BLOCK.finditer(content):
            config = match.group("config")
            url_match = re.search(r"\burl\s*:\s*([^,\n]+)", config)
            method_match = re.search(r"\bmethod\s*:\s*['\"]([A-Za-z]+)['\"]", config)
            if not url_match:
                continue
            function_name = match.group("name")
            url = normalize_expression(url_match.group(1))
            call_sites = find_call_sites(function_name, path, contents, source_root)
            calls.append(
                {
                    "method": (method_match.group(1) if method_match else "GET").upper(),
                    "path": url,
                    "function": function_name,
                    "definition_file": str(path.relative_to(root)),
                    "definition_line": content[: match.start()].count("\n") + 1,
                    "call_sites": call_sites,
                }
            )
    return calls


def find_call_sites(function_name: str, definition: Path, contents: dict, source_root: Path) -> list[dict]:
    pattern = re.compile(rf"\b{re.escape(function_name)}\s*\(")
    sites = []
    for path, content in contents.items():
        if path == definition:
            continue
        for match in pattern.finditer(content):
            sites.append(
                {
                    "file": str(path.relative_to(source_root.parent)),
                    "line": content[: match.start()].count("\n") + 1,
                }
            )
    return sites[:30]


def normalize_expression(expression: str) -> str:
    value = expression.strip().rstrip(")")
    literals = re.findall(r"['\"]([^'\"]*)['\"]", value)
    if not literals:
        return value.strip("`'\"")
    joined = "".join(literals)
    dynamic_count = max(value.count("+") - len(literals) + 1, 0)
    if "+" in value and not joined.endswith("}"):
        joined += "{param}" * max(dynamic_count, 1)
    return joined or "/"


def attach_frontend_callers(endpoints: list[dict], calls: list[dict]) -> None:
    for endpoint in endpoints:
        endpoint_pattern = path_pattern(endpoint.get("path", ""))
        endpoint["frontend_callers"] = [
            call
            for call in calls
            if call["method"] == endpoint.get("method", "").upper()
            and path_pattern(call["path"]) == endpoint_pattern
        ]


def path_pattern(path: str) -> str:
    value = re.sub(r"https?://[^/]+", "", path).split("?", 1)[0]
    value = re.sub(r"\{[^}]+\}|:[\w]+", "{}", value)
    value = re.sub(r"/+", "/", value)
    return value.rstrip("/") or "/"
