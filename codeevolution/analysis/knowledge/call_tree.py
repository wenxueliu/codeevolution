"""Per-endpoint call-chain tree service.

Serves, **one node at a time**, the children a user sees when expanding a node
of an endpoint's call-chain tree:

  * ``func``     — an intra-repo function/method the current node calls
  * ``cross``    — an outbound HTTP call matched to a downstream service's
                   inbound endpoint (re-expandable into that service's subtree)
  * ``external`` — an outbound HTTP call that could not be matched to any
                   registered service (external / unknown target)

The tree is rooted at an endpoint handler and expands lazily on demand, so the
full call graph is never materialised. Single-repo edges come straight from
CodeGraph ``calls``; cross-service splicing reuses the topology layer's URL
extraction + path matching against an on-the-fly inbound-API index of the other
registered logical services.
"""

from __future__ import annotations

from pathlib import Path

from ...infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository
from ...registry import get_repo, list_repos, repository_members
from ..topology.cross_repo_impl import HTTP_CLIENT_CALLERS, extract_outbound_url
from ..topology.matching import PathMatcher
from .api_contract import ApiContractExtractor

# Children returned per expanded node; the rest are dropped with truncated=True.
CHILD_CAP = 60
# Direct callees read from the graph in one query (bounded for giant functions).
MAX_CALLEE_ROWS = 200

# inbound API index cache: (logical service name, db fingerprint) -> endpoint list.
_INBOUND_CACHE: dict[tuple[tuple[str, ...], ...], list[dict]] = {}


def _member_key(member: dict) -> str:
    return str(member.get("name") or Path(member["path"]).name)


def _http_method_for(callee_name: str, language: str) -> str | None:
    """Return the HTTP method inferred for an outbound HTTP client callee, or None.

    Matches the same ``HTTP_CLIENT_CALLERS`` name patterns the topology layer
    uses (SQL ``LIKE``), normalising ``got(``-style entries to their base name.
    """
    if not callee_name:
        return None
    lang_key = (language or "").lower()
    if lang_key in HTTP_CLIENT_CALLERS:
        pattern_lists = [HTTP_CLIENT_CALLERS[lang_key]]
    else:
        pattern_lists = list(HTTP_CLIENT_CALLERS.values())
    for patterns in pattern_lists:
        for pattern, method in patterns:
            probe = pattern.rstrip("(")
            if probe and probe in callee_name:
                return method
    return None


def _codegraph_db(member: dict) -> Path:
    return Path(member["path"]) / ".codegraph" / "codegraph.db"


class CallTreeService:
    """Lazy children lookup for one node of a call-chain tree."""

    def __init__(self, entries: list[dict] | None = None):
        self._entries = list_repos() if entries is None else entries

    # ── public API ─────────────────────────────────────────────────────

    def expand(
        self,
        service: str,
        member: str | None = None,
        *,
        file: str | None = None,
        line: int | None = None,
        node_id: str | None = None,
        handler: str | None = None,
    ) -> dict:
        """Return the children of the requested node within ``service``.

        ``service`` is the logical service name. Exactly one node descriptor
        must identify the node to expand:

          * ``handler``    — cross-service expansion by downstream handler qname
          * ``node_id``    — intra-repo function node
          * ``file+line``  — endpoint handler root (resolved by location)

        ``member`` selects the physical repository for intra-repo lookups
        (optional when the service has a single member).
        """
        member_dict, db_path = self._resolve_db(service, member, handler=handler)
        if db_path is None:
            return {"children": [], "root": None, "truncated": False}

        with SQLiteCodeGraphRepository(db_path) as reader:
            node = self._resolve_node(
                reader,
                member=member_dict,
                file=file,
                line=line,
                node_id=node_id,
                handler=handler,
            )
            if node is None:
                return {"children": [], "root": None, "truncated": False}
            root = self._node_view(node)
            root["member"] = _member_key(member_dict) if member_dict else None
            children, truncated = self._children(
                reader, db_path, service, _member_key(member_dict) if member_dict else None, node
            )
        return {"children": children, "root": root, "truncated": truncated}

    # ── resolution helpers ─────────────────────────────────────────────

    def _resolve_db(
        self, service: str, member: str | None, handler: str | None = None
    ) -> tuple[dict | None, str | None]:
        """Pick the physical member (and its codegraph db) to expand within."""
        entry = next((e for e in self._entries if e.get("name") == service), None)
        if entry is None:
            entry = get_repo(service)
        if not entry:
            return None, None
        members = repository_members(entry)
        if not members:
            return None, None

        if handler is not None:
            chosen = None
            for m in members:
                db = _codegraph_db(m)
                if not db.exists():
                    continue
                with SQLiteCodeGraphRepository(str(db)) as reader:
                    if reader.function_node(handler) is not None:
                        chosen = m
                        break
            member_dict = chosen
        elif member:
            member_dict = next((m for m in members if _member_key(m) == member), None)
        elif len(members) == 1:
            member_dict = members[0]
        else:
            member_dict = None

        if member_dict is None:
            return None, None
        db_path = _codegraph_db(member_dict)
        return (member_dict, str(db_path)) if db_path.exists() else (None, None)

    @staticmethod
    def _resolve_node(
        reader: SQLiteCodeGraphRepository,
        *,
        member: dict | None,
        file: str | None,
        line: int | None,
        node_id: str | None,
        handler: str | None,
    ) -> dict | None:
        """Resolve a node row (id, name, qualified_name, file_path, …) in ``reader``."""
        del member  # db already selected by _resolve_db
        if handler:
            return reader.function_node(handler)
        if node_id:
            fn = reader.get_function_by_id(node_id)
            if fn is None:
                return None
            return {
                "id": fn.node_id,
                "name": fn.name,
                "qualified_name": fn.qualified_name,
                "kind": fn.kind,
                "file_path": fn.file_path,
                "start_line": fn.start_line,
                "end_line": fn.end_line,
                "signature": fn.signature,
            }
        if file is not None and line is not None:
            return reader.handler_for_route(file, int(line))
        return None

    # ── children computation ───────────────────────────────────────────

    def _children(
        self,
        reader: SQLiteCodeGraphRepository,
        db_path: str,
        service: str,
        member: str | None,
        node: dict,
    ) -> tuple[list[dict], bool]:
        language = reader.primary_language()
        caller_qname = node.get("qualified_name") or node.get("name") or ""
        callees = reader.get_callee_rows(node.get("id") or "", MAX_CALLEE_ROWS)

        func_rows: list[dict] = []
        http_rows: list[dict] = []
        for row in callees:
            name = row.get("name") or ""
            if _http_method_for(name, language) is not None:
                http_rows.append(row)
            else:
                func_rows.append(row)

        children: list[dict] = []
        for row in func_rows:
            children.append(
                {
                    "type": "func",
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "qualified_name": row.get("qualified_name"),
                    "kind": row.get("kind"),
                    "file": row.get("file_path"),
                    "line": row.get("start_line"),
                    "call_line": row.get("call_line"),
                    "signature": row.get("signature"),
                    "member": member,
                    "expandable": True,
                }
            )

        for row in http_rows:
            child = self._http_child(db_path, service, caller_qname, row)
            if child:
                children.append(child)

        truncated = len(children) > CHILD_CAP
        return children[:CHILD_CAP], truncated

    def _http_child(self, db_path: str, service: str, caller_qname: str, row: dict) -> dict | None:
        """Turn an HTTP-client callee into a cross or external child node."""
        method = _http_method_for(row.get("name") or "", "")
        language_agnostic_method = method  # fallback; refined below if needed
        # Method was detected earlier during scan; keep the same inference.
        raw_url = extract_outbound_url(db_path, caller_qname, row.get("call_line"))
        path = PathMatcher.extract(raw_url) if raw_url else None
        if not path:
            return {
                "type": "external",
                "id": f"e:{row.get('id')}",
                "name": row.get("name") or "",
                "http_method": language_agnostic_method or "",
                "url": raw_url or "",
                "file": row.get("file_path"),
                "line": row.get("start_line"),
                "call_line": row.get("call_line"),
                "note": "出站 HTTP 调用，未解析到 URL 或未匹配到下游服务",
                "expandable": False,
            }

        target = self._match_target(service, language_agnostic_method, path)
        if target:
            return {
                "type": "cross",
                "id": f"x:{target['service']}:{target['function'] or target['path']}",
                "name": f"{target['method'] or language_agnostic_method or 'HTTP'} {target['path']}",
                "http_method": target["method"] or language_agnostic_method or "HTTP",
                "url": raw_url or "",
                "url_pattern": target["path"],
                "target_service": target["service"],
                "target_function": target["function"],
                "target_file": target["file"],
                "target_line": target["line"],
                "expandable": bool(target["function"]),
            }
        return {
            "type": "external",
            "id": f"e:{row.get('id')}",
            "name": row.get("name") or "",
            "http_method": language_agnostic_method or "",
            "url": raw_url or "",
            "file": row.get("file_path"),
            "line": row.get("start_line"),
            "call_line": row.get("call_line"),
            "note": "出站 HTTP 调用（未匹配到已注册服务）",
            "expandable": False,
        }

    def _match_target(self, service: str, method: str | None, path: str) -> dict | None:
        """Match an outbound path+method against other services' inbound APIs."""
        for entry in self._entries:
            other = entry.get("name")
            if not other or other == service:
                continue
            for ep in self._inbound_endpoints(entry):
                api_method = ep.get("method") or ""
                api_path = ep.get("path") or ""
                if not api_path:
                    continue
                if method and api_method and method.upper() != api_method.upper():
                    continue
                if not PathMatcher.matches(path, api_path):
                    continue
                return {
                    "service": other,
                    "function": ep.get("handler") or "",
                    "path": api_path,
                    "method": api_method,
                    "file": ep.get("file") or "",
                    "line": ep.get("line") or 0,
                }
        return None

    def _inbound_endpoints(self, entry: dict) -> list[dict]:
        """Other services' inbound API endpoints, cached per db fingerprint."""
        members = repository_members(entry)
        fps = [str(_codegraph_db(m)) + ":" + self._fingerprint(_codegraph_db(m)) for m in members]
        key = ((entry.get("name") or ""), *sorted(fps))
        cached = _INBOUND_CACHE.get(key)
        if cached is not None:
            return cached

        endpoints: list[dict] = []
        for m in members:
            db = _codegraph_db(m)
            if not db.exists():
                continue
            with SQLiteCodeGraphRepository(str(db)) as reader:
                api = ApiContractExtractor(reader).extract()
            endpoints.extend(
                {
                    "method": ep.method,
                    "path": ep.path,
                    "handler": ep.handler_name,
                    "file": ep.file_path,
                    "line": ep.line,
                }
                for ep in api.endpoints
            )
        _INBOUND_CACHE[key] = endpoints
        return endpoints

    @staticmethod
    def _fingerprint(db: Path) -> str:
        try:
            stat = db.stat()
            return f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            return "missing"

    @staticmethod
    def _node_view(node: dict) -> dict:
        return {
            "type": "func",
            "id": node.get("id"),
            "name": node.get("name"),
            "qualified_name": node.get("qualified_name"),
            "kind": node.get("kind"),
            "file": node.get("file_path"),
            "line": node.get("start_line"),
            "signature": node.get("signature"),
        }
