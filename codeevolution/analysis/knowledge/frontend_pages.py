"""Frontend page discovery from router configs and page/view components."""

from __future__ import annotations

import re
from typing import Any

from ...ports import SourceProvider

_PAGE_EXTENSIONS = {".vue", ".jsx", ".tsx", ".js", ".ts"}
_PAGE_DIRS = ("/views/", "/pages/", "/screens/")
_ROUTE_FILE_HINTS = ("router", "routes")
# route: 'xxx' / path: "xxx" inside router configs
_ROUTE_PATH = re.compile(r"\b(?:path|route)\s*:\s*['\"`]([^'\"`]+)['\"`]")
_ROUTE_NAME = re.compile(r"\bname\s*:\s*['\"]([\w\-]+)['\"]")
_ROUTE_TITLE = re.compile(r"\btitle\s*:\s*['\"]([^'\"]{1,60})['\"]")
# UI chrome that surrounds pages but is never a business surface
_CHROME_TOKENS = {"layout", "frame", "shell", "container", "wrapper", "template", "blank", "skeleton"}


def _is_chrome(name: str) -> bool:
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+", name or "")
    lowered = [part.lower() for part in parts]
    return not lowered or lowered[0] in _CHROME_TOKENS or lowered[-1] in _CHROME_TOKENS


class FrontendPageExtractor:
    """Discover UI pages from frozen sources: router entries plus page/view
    components.  Emits one record per page with its route, display title and
    defining file so downstream consumers get a traceable source."""

    def __init__(self, source: SourceProvider):
        self.source = source

    def extract(self) -> list[dict[str, Any]]:
        pages: dict[str, dict[str, Any]] = {}
        route_tables = self._route_tables()
        for route in route_tables:
            name = route.get("name") or self._name_from_route_path(route.get("path", ""))
            if not name or _is_chrome(name):
                continue
            key = name.lower()
            record = pages.setdefault(key, {"name": name, "route": "", "title": "", "file": ""})
            record["route"] = record["route"] or route.get("path", "")
            record["title"] = record["title"] or route.get("title", "")
        for path, stem in self._page_components():
            key = stem.lower()
            record = pages.setdefault(key, {"name": stem, "route": "", "title": "", "file": ""})
            record["file"] = record["file"] or path
            if not record["name"]:
                record["name"] = stem
        return [
            {
                "name": record["name"],
                "route": record["route"],
                "title": record["title"],
                "file": record["file"],
            }
            for _, record in sorted(pages.items())
        ]

    def _frontend_files(self) -> list[tuple[str, str]]:
        files = []
        try:
            listed = self.source.list_files()
        except Exception:
            return files
        for item in listed:
            path = getattr(item, "path", None) or (item.get("path") if isinstance(item, dict) else None)
            if not path or path.rsplit(".", 1)[-1].lower() not in {ext.lstrip(".") for ext in _PAGE_EXTENSIONS}:
                continue
            marked = f"/{path.replace(chr(92), '/')}"
            if not any(marker in marked for marker in (*_PAGE_DIRS, "/router", "/routes")):
                continue
            text = self.source.read_text(path)
            if text:
                files.append((path, text))
        return files

    def _route_tables(self) -> list[dict[str, str]]:
        routes = []
        for path, text in self._frontend_files():
            lower = path.lower()
            if not any(hint in lower for hint in _ROUTE_FILE_HINTS):
                continue
            for block in self._route_blocks(text):
                route: dict[str, str] = {"file": path}
                path_match = _ROUTE_PATH.search(block)
                if path_match:
                    route["path"] = path_match.group(1)
                name_match = _ROUTE_NAME.search(block)
                if name_match:
                    route["name"] = name_match.group(1)
                title_match = _ROUTE_TITLE.search(block)
                if title_match:
                    route["title"] = title_match.group(1)
                if route.get("name") or route.get("path"):
                    routes.append(route)
        return routes

    @staticmethod
    def _route_blocks(text: str) -> list[str]:
        """Split a router module into per-route chunks at `{ path:` boundaries,
        keeping the delimiter so each chunk still carries its path key."""
        parts = re.split(r"(\{\s*(?:path|route)\s*:)", text)
        if len(parts) == 1:
            return [text]
        return [parts[i] + parts[i + 1] for i in range(1, len(parts), 2)]

    def _page_components(self) -> list[tuple[str, str]]:
        components = []
        for path, _text in self._frontend_files():
            if not any(marker in f"/{path}" for marker in _PAGE_DIRS):
                continue
            stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if stem.lower() in {"index", "main", "app"} or _is_chrome(stem):
                continue
            components.append((path, stem))
        return components

    @staticmethod
    def _name_from_route_path(route_path: str) -> str:
        """Derive a page name from the route path when the table has no name:
        '/user/order-list' -> UserOrderList; parameter segments are skipped."""
        parts = [
            segment
            for segment in re.split(r"[-_/]", route_path.strip("/"))
            if segment and not segment.startswith(("{", ":"))
        ]
        return "".join(part[:1].upper() + part[1:] for part in parts[:4])
