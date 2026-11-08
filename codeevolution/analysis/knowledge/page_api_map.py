"""Page-to-API attribution from frozen frontend sources.

Links routing pages (``frontend_pages``) to the HTTP endpoints they reach:
resolve frontend import statements into a file-level import graph, compute
each page's reachable file set, and attribute HTTP call sites that fall
inside it.  Pure function of the frozen snapshot sources plus an optional
CodeGraph import-edge supplement; projected at the read boundary for older
snapshots.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any, Iterable

from .frontend_calls import HTTP_BLOCK, normalize_expression, path_pattern

PAGE_API_MAP_VERSION = 1

_FRONTEND_EXTENSIONS = (".js", ".ts", ".jsx", ".tsx", ".vue", ".mjs")
_API_DIR_MARKERS = ("/api/", "/apis/")
_MAX_CALL_SITES = 30

_IMPORT_RE = re.compile(
    r"(?:import\s[^;]*?from\s+|import\s*\(\s*|require\s*\(\s*)['\"]([^'\"]+)['\"]"
)
_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)


def extract_page_api_calls(
    source: Any,
    pages: list[dict],
    endpoints: list[dict],
    graph_import_edges: Iterable[Any] = (),
) -> dict:
    """Attribute HTTP calls to routing pages via frontend import reachability.

    ``source`` is any object exposing ``list_files()`` / ``read_text(path)``
    (frozen snapshot sources).  ``pages`` are ``frontend_pages`` records,
    ``endpoints`` the serialized API contract entries used only for the
    ``endpoint_matched`` flag.
    """
    code = _frontend_files(source)
    if not code:
        return _empty_result()

    file_set = set(code)
    stripped_index: dict[str, str] = {}
    for path in sorted(file_set):
        key = _strip_first(path)
        if key and key not in stripped_index:
            stripped_index[key] = path

    adjacency: dict[str, set[str]] = {path: set() for path in file_set}
    unresolved_imports = 0
    for path, text in code.items():
        for spec in _parse_imports(_script_code(path, text)):
            target = _resolve_import(spec, path, file_set, stripped_index)
            if target:
                adjacency[path].add(target)
            elif _is_relative_or_alias(spec):
                unresolved_imports += 1
    for edge in graph_import_edges:
        pair = _edge_pair(edge)
        if pair is None:
            continue
        f1, f2 = pair
        source_file = _canonical(f1, file_set, stripped_index)
        target_file = _canonical(f2, file_set, stripped_index)
        if source_file and target_file:
            adjacency[source_file].add(target_file)

    reach: dict[str, set[str]] = {}
    for page in pages:
        page_file = page.get("file") or ""
        if page_file in file_set and page_file not in reach:
            reach[page_file] = _reachable(page_file, adjacency)

    endpoint_index = {
        (str(item.get("method", "")).upper(), path_pattern(str(item.get("path", ""))))
        for item in endpoints
        if isinstance(item, dict)
    } if isinstance(endpoints, list) else set()

    calls = _extract_calls(code)
    mapped_pages: dict[str, dict] = {}
    unmapped: dict[tuple, dict] = {}
    for call in calls:
        attributed = False
        for page in pages:
            page_file = page.get("file") or ""
            reachable = reach.get(page_file)
            if not reachable:
                continue
            site = next(
                (site for site in call["call_sites"] if site["file"] in reachable),
                None,
            )
            if site is None and call["definition_file"] in reachable:
                site = {"file": call["definition_file"], "line": call["definition_line"]}
            if site is None:
                continue
            attributed = True
            record = mapped_pages.setdefault(
                page_file,
                {
                    "name": page.get("name", ""),
                    "route": page.get("route", ""),
                    "title": page.get("title", ""),
                    "file": page_file,
                    "endpoints": [],
                },
            )
            key = (call["method"], call["path"], call["function"])
            if any((e["method"], e["path"], e["function"]) == key for e in record["endpoints"]):
                continue
            record["endpoints"].append(
                {
                    "method": call["method"],
                    "path": call["path"],
                    "function": call["function"],
                    "call_site_file": site["file"],
                    "call_site_line": site["line"],
                    "definition_file": call["definition_file"],
                    "endpoint_matched": (call["method"], path_pattern(call["path"])) in endpoint_index,
                }
            )
        if not attributed:
            key = (call["method"], call["path"], call["function"], call["definition_file"])
            unmapped.setdefault(
                key,
                {
                    "method": call["method"],
                    "path": call["path"],
                    "function": call["function"],
                    "definition_file": call["definition_file"],
                },
            )

    mapped = [mapped_pages[key] for key in sorted(mapped_pages)]
    pages_without_calls = [
        {"name": page.get("name", ""), "file": page.get("file", "")}
        for page in pages
        if (page.get("file") or "") not in mapped_pages
    ]
    return {
        "version": PAGE_API_MAP_VERSION,
        "page_count": len(mapped),
        "pages": mapped,
        "unmapped_calls": sorted(unmapped.values(), key=lambda item: (item["path"], item["function"])),
        "pages_without_calls": pages_without_calls,
        "unresolved_imports": unresolved_imports,
        "orphan_call_count": len(unmapped),
    }


def _empty_result() -> dict:
    return {
        "version": PAGE_API_MAP_VERSION,
        "page_count": 0,
        "pages": [],
        "unmapped_calls": [],
        "pages_without_calls": [],
        "unresolved_imports": 0,
        "orphan_call_count": 0,
    }


def _frontend_files(source: Any) -> dict[str, str]:
    """Read all frontend files from a SourceProvider as {posix_path: text}."""
    try:
        listed = source.list_files()
    except Exception:
        return {}
    files: dict[str, str] = {}
    for item in listed:
        path = getattr(item, "path", None) or (item.get("path") if isinstance(item, dict) else None)
        if not path or path.rsplit(".", 1)[-1].lower() not in {ext.lstrip(".") for ext in _FRONTEND_EXTENSIONS}:
            continue
        try:
            text = source.read_text(path)
        except Exception:
            continue
        if text:
            files[path.replace("\\", "/")] = text
    return files


def _script_code(path: str, text: str) -> str:
    """Source text that may contain imports: the script block for Vue SFCs."""
    if not path.endswith(".vue"):
        return text
    return "\n".join(match.group(1) for match in _SCRIPT_RE.finditer(text))


def _parse_imports(code: str) -> list[str]:
    return _IMPORT_RE.findall(code)


def _is_relative_or_alias(spec: str) -> bool:
    return spec.startswith((".", "/", "@/")) or spec.startswith("~/")


def _strip_first(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[1:]) if len(parts) > 1 else ""


def _import_base(spec: str, importer: str) -> str | None:
    """Normalize an import specifier to an extensionless candidate path."""
    if spec.startswith("@/") or spec.startswith("~/"):
        return posixpath.normpath("src/" + spec[2:])
    if spec.startswith("."):
        return posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    if spec.startswith("/"):
        return posixpath.normpath(spec.lstrip("/"))
    return None  # bare npm package


def _candidates(base: str) -> list[str]:
    return [base, *(base + ext for ext in _FRONTEND_EXTENSIONS),
            *(f"{base}/index{ext}" for ext in _FRONTEND_EXTENSIONS)]


def _resolve_import(spec: str, importer: str, file_set: set[str], stripped_index: dict[str, str]) -> str | None:
    base = _import_base(spec, importer)
    if base is None:
        return None
    # Manifest paths carry a repo-prefix segment (e.g. ``web/src/...``) while
    # alias roots resolve to ``src/...``; try the base both with and without
    # that prefix so both forms meet in the same index.
    bases = [base]
    stripped = _strip_first(base)
    if stripped and stripped not in bases:
        bases.append(stripped)
    for root in bases:
        for candidate in _candidates(root):
            if candidate in file_set:
                return candidate
            if candidate in stripped_index:
                return stripped_index[candidate]
    return None


def _canonical(path: str, file_set: set[str], stripped_index: dict[str, str]) -> str | None:
    if path in file_set:
        return path
    return stripped_index.get(_strip_first(path))


def _edge_pair(edge: Any) -> tuple[str, str] | None:
    if isinstance(edge, dict):
        f1, f2 = edge.get("f1"), edge.get("f2")
    elif isinstance(edge, (tuple, list)) and len(edge) == 2:
        f1, f2 = edge[0], edge[1]
    else:
        return None
    if isinstance(f1, str) and isinstance(f2, str) and f1 and f2:
        return f1, f2
    return None


def _reachable(start: str, adjacency: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(adjacency.get(current, ()))
    return seen


def _is_api_file(path: str) -> bool:
    marked = f"/{path.replace(chr(92), '/')}"
    return any(marker in marked for marker in _API_DIR_MARKERS)


def _extract_calls(code: dict[str, str]) -> list[dict]:
    """HTTP-call extraction from frozen text, mirroring extract_frontend_calls."""
    calls: list[dict] = []
    for path, text in code.items():
        if not _is_api_file(path):
            continue
        for match in HTTP_BLOCK.finditer(text):
            config = match.group("config")
            url_match = re.search(r"\burl\s*:\s*([^,\n]+)", config)
            if not url_match:
                continue
            method_match = re.search(r"\bmethod\s*:\s*['\"]([A-Za-z]+)['\"]", config)
            function_name = match.group("name")
            calls.append(
                {
                    "method": (method_match.group(1) if method_match else "GET").upper(),
                    "path": normalize_expression(url_match.group(1)),
                    "function": function_name,
                    "definition_file": path,
                    "definition_line": text[: match.start()].count("\n") + 1,
                    "call_sites": _call_sites(function_name, path, code),
                }
            )
    return calls


def _call_sites(function_name: str, definition: str, code: dict[str, str]) -> list[dict]:
    pattern = re.compile(rf"\b{re.escape(function_name)}\s*\(")
    sites: list[dict] = []
    for path, text in code.items():
        if path == definition:
            continue
        for match in pattern.finditer(text):
            sites.append({"file": path, "line": text[: match.start()].count("\n") + 1})
    return sites[:_MAX_CALL_SITES]


__all__ = ["PAGE_API_MAP_VERSION", "extract_page_api_calls"]
